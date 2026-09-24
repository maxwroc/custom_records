"""
Storage layer for custom_records: one SQLite database per config entry.

Callers (services.py, websocket_api.py, config_flow.py, media_source.py,
media_store.py, __init__.py) never execute SQL directly - they call these
domain methods, which return the same `{id, t, d}` envelope shape the old
in-memory/HA-Store implementation used (see record_view.py), even though rows
now live in a real `STRICT` SQLite table per record type (plan_sql.md Phase
1-2). All blocking sqlite3 calls run on one dedicated single-worker executor
per entry, never on HA's shared executor pool, since a sqlite3 connection
opened with `check_same_thread=True` may only be used from the thread that
created it.

Every generated SQL statement below interpolates ONLY identifiers that were
already validated (`RECORD_TYPE_ID_PATTERN`/`FIELD_KEY_PATTERN`, see
models.py) and passed through `quote_identifier` - actual values are always
bound as `?` parameters, never interpolated. `# noqa: S608` markers on those
statements are a deliberate, reviewed exception to that lint rule, not a
blanket suppression - see `models.py`'s `_validate_identifier` for the
matching input validation.
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ENTRY_ID,
    ATTR_RECORD_TYPE,
    COL_ID,
    COL_TIMESTAMP,
    DB_FILENAME_TEMPLATE,
    DB_SCHEMA_VERSION,
    DEFAULT_HISTOGRAM_BIN_COUNT,
    DOMAIN,
    ENVELOPE_DATA,
    ENVELOPE_ID,
    ENVELOPE_TIMESTAMP,
    EVENT_RECORDS_UPDATED,
    LOGGER,
    MAX_HISTOGRAM_BINS,
    SQL_TYPE_FOR_FIELD_TYPE,
    AggregateBucket,
    AggregateOp,
    FieldType,
    RecordOrder,
)
from .pagination import RecordPosition
from .sql_encoding import (
    CompiledFilter,
    decode_field,
    encode_field,
    from_epoch_micros,
    is_finite_number,
    quote_identifier,
    to_epoch_micros,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from .csv_transfer import ImportRow
    from .models import FieldDefinition, RecordType


@dataclass
class ImportSummary:
    """Result of a bulk CSV import (store.py's async_import_records)."""

    imported: int
    skipped_duplicate: int


@dataclass(frozen=True, slots=True)
class RecordsPage:
    """Live records and an optional continuation key for a limited read."""

    records: list[dict[str, Any]]
    next_position: RecordPosition | None


class SchemaError(RuntimeError):
    """Raised when a configured record type's table is missing/mismatched."""


def _freeze(value: Any) -> Any:
    """
    Recursively convert a field-data dict/list into a hashable form.

    Used to build a set of (timestamp, data) "signatures" for content-based
    duplicate detection in async_import_records - a plain dict/list isn't
    hashable, so nested containers are converted into tuples (dict items
    sorted by key, so key order never affects equality/hashing).
    """
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _column_ddl(field_def: FieldDefinition) -> str:
    """Build one column's DDL fragment, including its type and constraints."""
    sql_type = SQL_TYPE_FOR_FIELD_TYPE[field_def.type]
    col = quote_identifier(field_def.sql_column)
    parts = [col, sql_type]
    if field_def.required:
        parts.append("NOT NULL")
    if field_def.type is FieldType.BOOLEAN:
        parts.append(f"CHECK ({col} IN (0, 1))")
    if field_def.type is FieldType.MULTI_SELECT:
        parts.append(
            f"CHECK ({col} IS NULL OR "
            f"(json_valid({col}) AND json_type({col}) = 'array'))"
        )
    return " ".join(parts)


def _create_table_sql(record_type: RecordType) -> str:
    """Build the `CREATE TABLE IF NOT EXISTS ... STRICT` DDL for a record type."""
    table = quote_identifier(record_type.sql_table)
    columns = [
        f"{quote_identifier(COL_ID)} TEXT PRIMARY KEY NOT NULL",
        f"{quote_identifier(COL_TIMESTAMP)} INTEGER NOT NULL",
        *[_column_ddl(f) for f in record_type.fields],
    ]
    return f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)}) STRICT"


def _index_sql(record_type: RecordType) -> str:
    """Build the `(timestamp, id)` index DDL for a record type's table."""
    index_name = f"idx_{record_type.sql_table}_ts"
    return (
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(index_name)} "
        f"ON {quote_identifier(record_type.sql_table)} "
        f"({quote_identifier(COL_TIMESTAMP)}, {quote_identifier(COL_ID)})"
    )


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({quote_identifier(table)})")
    return {row[1] for row in cursor.fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return whether a table exists in the main schema."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _validate_table_sync(conn: sqlite3.Connection, record_type: RecordType) -> None:
    """Validate a configured table's columns, types, nullability, and index."""
    if not _table_exists(conn, record_type.sql_table):
        msg = f"Configured record type '{record_type.id}' is missing its database table"
        raise SchemaError(msg)

    table_info = {
        row[1]: (row[2], bool(row[3]), row[5])
        for row in conn.execute(
            f"PRAGMA table_xinfo({quote_identifier(record_type.sql_table)})"
        ).fetchall()
    }
    expected = {
        COL_ID: ("TEXT", True, 1),
        COL_TIMESTAMP: ("INTEGER", True, 0),
        **{
            field_def.sql_column: (
                SQL_TYPE_FOR_FIELD_TYPE[field_def.type],
                field_def.required,
                0,
            )
            for field_def in record_type.fields
        },
    }
    for column, contract in expected.items():
        if table_info.get(column) != contract:
            msg = (
                f"Configured record type '{record_type.id}' has a mismatched "
                f"database column '{column}'"
            )
            raise SchemaError(msg)

    table_row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (record_type.sql_table,),
    ).fetchone()
    if table_row is None or not table_row[0].rstrip().upper().endswith(" STRICT"):
        msg = f"Configured record type '{record_type.id}' is not a STRICT table"
        raise SchemaError(msg)

    expected_index = f"idx_{record_type.sql_table}_ts"
    indexes = {
        row[1]
        for row in conn.execute(
            f"PRAGMA index_list({quote_identifier(record_type.sql_table)})"
        ).fetchall()
    }
    if expected_index not in indexes:
        msg = (
            f"Configured record type '{record_type.id}' is missing its timestamp index"
        )
        raise SchemaError(msg)


def _ensure_table_sync(conn: sqlite3.Connection, record_type: RecordType) -> None:
    """
    Create a record type's table/index if missing; add any new optional columns.

    Covers both the "new record type" and "add field" idempotent-DDL cases
    from plan_sql.md Phase 1 pt.7: re-running this for an already-current
    table is a safe no-op. A configured REQUIRED field missing from an
    existing table is a structural mismatch (data loss risk), never silently
    patched - see plan_sql.md Phase 1 pt.6/7.
    """
    conn.execute(_create_table_sql(record_type))
    conn.execute(_index_sql(record_type))
    existing = _existing_columns(conn, record_type.sql_table)
    for field_def in record_type.fields:
        if field_def.sql_column in existing:
            continue
        if field_def.required:
            msg = (
                f"Record type '{record_type.id}' field '{field_def.key}' is "
                "required but missing from its existing table; only optional "
                "fields can be added to an existing record type"
            )
            raise SchemaError(msg)
        conn.execute(
            f"ALTER TABLE {quote_identifier(record_type.sql_table)} "
            f"ADD COLUMN {_column_ddl(field_def)}"
        )
    conn.commit()


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Generator[None]:
    """Run a transaction and leave the connection reusable after any failure."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                LOGGER.exception("Failed to roll back SQLite transaction")
        raise


def _open_sync(db_path: Path) -> tuple[sqlite3.Connection, bool]:
    """Open (creating if needed) the entry's database with explicit pragmas."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None disables sqlite3's implicit transaction handling so
    # every transaction boundary below is an explicit BEGIN/COMMIT/ROLLBACK
    # (plan_sql.md Phase 2 pt.11).
    conn = sqlite3.connect(db_path, check_same_thread=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    # Rollback-journal (default) mode with NORMAL synchronous - see
    # plan_sql.md Phase 2 pt.13 for why WAL is deferred.
    conn.execute("PRAGMA synchronous = NORMAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    is_new_database = version == 0
    if version == 0:
        conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
    elif version > DB_SCHEMA_VERSION:
        conn.close()
        msg = (
            f"Custom Records database schema version {version} is newer than "
            f"supported ({DB_SCHEMA_VERSION}); refusing to open it"
        )
        raise SchemaError(msg)
    return conn, is_new_database


def _bucket_expr(bucket: AggregateBucket | int) -> str:
    """
    Build the SQL expression computing a bucket-start label.

    `bucket` is either a named calendar-aware `AggregateBucket` (UTC day/
    week/month, unchanged) or a plain `int` of seconds - a custom fixed-size,
    epoch-aligned bucket (minutes/hours only, see const.py
    `CUSTOM_BUCKET_PATTERN`'s docstring for why day/week+ stay calendar-only).
    """
    seconds_expr = f"({quote_identifier(COL_TIMESTAMP)} / 1000000.0)"
    if isinstance(bucket, int):
        return (
            "strftime('%Y-%m-%dT%H:%M:%S+00:00', "
            f"CAST({seconds_expr} / {bucket} AS INTEGER) * {bucket}, 'unixepoch')"
        )
    if bucket is AggregateBucket.HOUR:
        return f"strftime('%Y-%m-%dT%H:00:00+00:00', {seconds_expr}, 'unixepoch')"
    if bucket is AggregateBucket.DAY:
        return f"strftime('%Y-%m-%dT00:00:00+00:00', {seconds_expr}, 'unixepoch')"
    if bucket is AggregateBucket.WEEK:
        # 'weekday 1' advances forward to Monday; '-6 days' first so a date
        # already on/after Monday lands on the Monday of its OWN week - the
        # standard SQLite recipe for "beginning of week".
        return (
            f"strftime('%Y-%m-%dT00:00:00+00:00', {seconds_expr}, 'unixepoch', "
            "'-6 days', 'weekday 1')"
        )
    if bucket is AggregateBucket.MONTH:
        return f"strftime('%Y-%m-01T00:00:00+00:00', {seconds_expr}, 'unixepoch')"
    msg = f"Unsupported bucket '{bucket}'"
    raise ValueError(msg)


def _build_where_clause(
    start: datetime | None, end: datetime | None, where: CompiledFilter | None
) -> tuple[str, list[Any]]:
    """Build a shared ` WHERE ...` SQL fragment (or "") from range/filter params."""
    conditions: list[str] = []
    params: list[Any] = []
    if start is not None:
        conditions.append(f"{quote_identifier(COL_TIMESTAMP)} >= ?")
        params.append(to_epoch_micros(dt_util.as_utc(start)))
    if end is not None:
        conditions.append(f"{quote_identifier(COL_TIMESTAMP)} <= ?")
        params.append(to_epoch_micros(dt_util.as_utc(end)))
    if where is not None:
        conditions.append(f"({where.sql})")
        params.extend(where.params)
    where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where_sql, params


def _validate_histogram_bounds(
    min_override: float | None, max_override: float | None
) -> None:
    """Validate optional explicit histogram bounds."""
    if any(
        value is not None and not is_finite_number(value)
        for value in (min_override, max_override)
    ):
        msg = "Histogram bounds must be finite"
        raise ValueError(msg)
    if (
        min_override is not None
        and max_override is not None
        and min_override >= max_override
    ):
        msg = "Histogram min must be less than max"
        raise ValueError(msg)


def _add_histogram_bounds(
    where_sql: str,
    params: list[Any],
    column: str,
    min_bound: float | None,
    max_bound: float | None,
) -> tuple[str, list[Any]]:
    """Append optional inclusive histogram bounds to a WHERE clause."""
    bounded_sql = where_sql
    bounded_params = list(params)
    if min_bound is not None:
        bounded_sql += f" AND {column} >= ?"
        bounded_params.append(min_bound)
    if max_bound is not None:
        bounded_sql += f" AND {column} <= ?"
        bounded_params.append(max_bound)
    return bounded_sql, bounded_params


@dataclass
class MetricSpec:
    """One requested aggregate metric in an `aggregate_records` call."""

    op: AggregateOp
    field_key: str | None
    name: str


def _resolve_metric_exprs(
    record_type: RecordType, metrics: list[MetricSpec]
) -> list[tuple[str, str]]:
    """Return (value_expr, count_expr) SQL fragments per metric."""
    exprs: list[tuple[str, str]] = []
    for metric in metrics:
        if metric.op is AggregateOp.COUNT:
            exprs.append(("COUNT(*)", "COUNT(*)"))
            continue
        field_def = (
            record_type.get_field(metric.field_key) if metric.field_key else None
        )
        if field_def is None:
            msg = f"Unknown field '{metric.field_key}'"
            raise ValueError(msg)
        col = quote_identifier(field_def.sql_column)
        exprs.append((f"{metric.op.value.upper()}({col})", f"COUNT({col})"))
    return exprs


def _resolve_group_by(
    record_type: RecordType, group_by_field_key: str | None
) -> tuple[str, str, FieldDefinition | None]:
    """Return (select_expr, from_extra, field_def) for an optional group_by."""
    if group_by_field_key is None:
        return "", "", None
    field_def = record_type.get_field(group_by_field_key)
    if field_def is None:
        msg = f"Unknown field '{group_by_field_key}'"
        raise ValueError(msg)
    col = quote_identifier(field_def.sql_column)
    if field_def.type is FieldType.MULTI_SELECT:
        # Explode: one group per individual selected value (a record can
        # land in multiple groups). json_each() silently skips a NULL
        # column (verified empirically) rather than erroring, so records
        # with the field unset simply contribute to no group.
        return "je.value AS group_value", f", json_each({col}) je", field_def
    return f"{col} AS group_value", "", field_def


def _wrap_cumulative(
    base_sql: str, metrics: list[MetricSpec], group_cols: list[str]
) -> str:
    """Wrap a grouped aggregate query with running-total window functions."""
    partition_sql = " PARTITION BY group_value" if "group_value" in group_cols else ""
    window = (
        f"OVER ({partition_sql} ORDER BY bucket "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    )
    outer_parts = list(group_cols)
    for i, metric in enumerate(metrics):
        if metric.op is AggregateOp.AVG:
            outer_parts.append(
                f"SUM(value_{i} * n_{i}) {window} "
                f"/ NULLIF(SUM(n_{i}) {window}, 0) AS value_{i}"
            )
        elif metric.op is AggregateOp.MIN:
            outer_parts.append(f"MIN(value_{i}) {window} AS value_{i}")
        elif metric.op is AggregateOp.MAX:
            outer_parts.append(f"MAX(value_{i}) {window} AS value_{i}")
        else:  # SUM, COUNT
            outer_parts.append(f"SUM(value_{i}) {window} AS value_{i}")
        outer_parts.append(f"SUM(n_{i}) {window} AS n_{i}")
    order_by_sql = f" ORDER BY {', '.join(group_cols)}"
    outer_sql = ", ".join(outer_parts)
    # base_sql/outer_parts are built only from validated/quoted identifiers;
    # the only bound values are the WHERE clause's own "?" params.
    return f"WITH agg AS ({base_sql}) SELECT {outer_sql} FROM agg{order_by_sql}"  # noqa: S608


def _build_aggregate_sql(  # noqa: PLR0913 (one param per query dimension)
    record_type: RecordType,
    metrics: list[MetricSpec],
    bucket: AggregateBucket | int | None,
    group_by_field_key: str | None,
    where_sql: str,
    *,
    cumulative: bool,
) -> tuple[str, list[str], FieldDefinition | None]:
    """Build the aggregate SQL statement; returns (sql, group_cols, group_field_def)."""
    select_parts: list[str] = []
    group_cols: list[str] = []
    if bucket is not None:
        select_parts.append(f"{_bucket_expr(bucket)} AS bucket")
        group_cols.append("bucket")

    group_select, from_extra, group_field_def = _resolve_group_by(
        record_type, group_by_field_key
    )
    if group_select:
        select_parts.append(group_select)
        group_cols.append("group_value")

    for i, (value_expr, count_expr) in enumerate(
        _resolve_metric_exprs(record_type, metrics)
    ):
        select_parts.append(f"{value_expr} AS value_{i}")
        select_parts.append(f"{count_expr} AS n_{i}")

    table = quote_identifier(record_type.sql_table)
    group_by_sql = f" GROUP BY {', '.join(group_cols)}" if group_cols else ""
    having_sql = ""
    if group_cols:
        having_terms = " OR ".join(f"n_{i} > 0" for i in range(len(metrics)))
        having_sql = f" HAVING {having_terms}"
    # select_parts/table/from_extra are built only from validated/quoted
    # identifiers; where_sql's own values are always "?" params.
    base_sql = (
        f"SELECT {', '.join(select_parts)} FROM {table}{from_extra}{where_sql}"  # noqa: S608
        f"{group_by_sql}{having_sql}"
    )

    if cumulative:
        sql = _wrap_cumulative(base_sql, metrics, group_cols)
    else:
        order_by_sql = f" ORDER BY {', '.join(group_cols)}" if group_cols else ""
        sql = base_sql + order_by_sql
    return sql, group_cols, group_field_def


def _aggregate_row_to_result(
    row: sqlite3.Row,
    metrics: list[MetricSpec],
    group_cols: list[str],
    group_field_def: FieldDefinition | None,
) -> dict[str, Any]:
    """Convert one raw aggregate SQL row into the normalized result dict."""
    bucket_val = row["bucket"] if "bucket" in group_cols else None
    group_val: Any = None
    if "group_value" in group_cols:
        group_val = row["group_value"]
        if (
            group_field_def is not None
            and group_field_def.type is not FieldType.MULTI_SELECT
        ):
            group_val = decode_field(group_field_def, group_val)
    metrics_result: dict[str, dict[str, Any]] = {}
    for i, metric in enumerate(metrics):
        value = row[f"value_{i}"]
        if isinstance(value, float) and not is_finite_number(value):
            msg = "Aggregate result is not a finite number"
            raise ValueError(msg)
        metrics_result[metric.name] = {"value": value, "count": row[f"n_{i}"]}
    return {"bucket": bucket_val, "group": group_val, "metrics": metrics_result}


class RecordStorage:
    """SQLite-backed record storage; one database file per config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the storage manager (does not open the database yet)."""
        self.hass = hass
        self.entry_id = entry_id
        self._db_path = Path(
            hass.config.path(
                ".storage", DOMAIN, DB_FILENAME_TEMPLATE.format(entry_id=entry_id)
            )
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"custom_records_db_{entry_id}"
        )
        self._conn: sqlite3.Connection | None = None
        self._record_types: dict[str, RecordType] = {}
        self._database_available = asyncio.Event()
        self._database_available.set()
        self._backup_in_progress = False
        self._closed = False

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run one blocking callable on this entry's dedicated DB worker thread."""
        await self._database_available.wait()
        if self._closed:
            msg = "RecordStorage is closed"
            raise RuntimeError(msg)
        return await self.hass.loop.run_in_executor(self._executor, func, *args)

    async def _run_direct(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run worker-thread work while normal operations are paused."""
        return await self.hass.loop.run_in_executor(self._executor, func, *args)

    async def _wait_until_available(self) -> None:
        """Wait until backup completes before looking up the active connection."""
        await self._database_available.wait()
        if self._closed:
            msg = "RecordStorage is closed"
            raise RuntimeError(msg)

    def _fire_updated(self, record_type_id: str) -> None:
        """Notify listeners (e.g. an open Lovelace card) that data changed."""
        self.hass.bus.async_fire(
            EVENT_RECORDS_UPDATED,
            {ATTR_ENTRY_ID: self.entry_id, ATTR_RECORD_TYPE: record_type_id},
        )

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            msg = "RecordStorage is not open; call async_load() first"
            raise RuntimeError(msg)
        return self._conn

    def _row_to_envelope(
        self, record_type: RecordType, row: sqlite3.Row
    ) -> dict[str, Any]:
        data = {
            field_def.key: decode_field(field_def, row[field_def.sql_column])
            for field_def in record_type.fields
        }
        timestamp = from_epoch_micros(row[COL_TIMESTAMP])
        return {
            ENVELOPE_ID: row[COL_ID],
            ENVELOPE_TIMESTAMP: timestamp.isoformat(),
            ENVELOPE_DATA: data,
        }

    # -- connection / schema lifecycle -------------------------------------

    async def async_open(self) -> None:
        """Open the database connection if not already open."""
        await self._wait_until_available()
        if self._conn is None:
            self._conn, _ = await self._run(_open_sync, self._db_path)

    async def async_load(self, record_types: dict[str, RecordType]) -> None:
        """
        Open the database and ensure a table exists for every configured type.

        Called on every setup/reload (including the reload HA triggers after
        a config subentry add/update/remove), so this idempotently creates
        any brand-new record type's table and adds any newly-configured
        optional field's column every time - see plan_sql.md Phase 1 pt.7.
        """
        await self._wait_until_available()
        if self._conn is None:
            self._conn, is_new_database = await self._run(_open_sync, self._db_path)
        else:
            is_new_database = False
        conn = self._require_conn()
        for record_type in record_types.values():
            operation = _ensure_table_sync if is_new_database else _validate_table_sync
            await self._run(operation, conn, record_type)
        self._record_types = dict(record_types)

    async def async_ensure_record_type(self, record_type: RecordType) -> None:
        """Create or add optional columns before publishing config metadata."""
        await self._wait_until_available()
        conn = self._require_conn()
        await self._run(_ensure_table_sync, conn, record_type)
        self._record_types[record_type.id] = record_type

    async def async_record_count(self, record_type_id: str) -> int:
        """Return the current row count for a record type."""
        await self._wait_until_available()
        record_type = self._record_types.get(record_type_id)
        if record_type is None:
            return 0
        conn = self._require_conn()

        def _count() -> int:
            table = quote_identifier(record_type.sql_table)
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608

        return await self._run(_count)

    # -- record CRUD ----------------------------------------------------------

    async def async_add_record(
        self,
        record_type_id: str,
        data: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Insert a new record in one transaction and fire the update event."""
        await self._wait_until_available()
        record_type = self._record_types[record_type_id]
        conn = self._require_conn()
        record_id = str(uuid4())
        ts = dt_util.as_utc(timestamp) if timestamp is not None else dt_util.utcnow()
        ts_micros = to_epoch_micros(ts)

        columns = [COL_ID, COL_TIMESTAMP]
        values: list[Any] = [record_id, ts_micros]
        for field_def in record_type.fields:
            if field_def.key not in data:
                continue
            columns.append(field_def.sql_column)
            values.append(encode_field(field_def, data[field_def.key]))

        def _insert() -> None:
            col_sql = ", ".join(quote_identifier(c) for c in columns)
            placeholders = ", ".join("?" for _ in columns)
            table = quote_identifier(record_type.sql_table)
            with _transaction(conn):
                conn.execute(
                    f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",  # noqa: S608
                    values,
                )

        await self._run(_insert)
        self._fire_updated(record_type_id)
        stored_data = {
            field_def.key: decode_field(
                field_def, encode_field(field_def, data.get(field_def.key))
            )
            for field_def in record_type.fields
        }
        return {
            ENVELOPE_ID: record_id,
            ENVELOPE_TIMESTAMP: ts.isoformat(),
            ENVELOPE_DATA: stored_data,
        }

    async def async_list_records(  # noqa: PLR0913
        self,
        record_type_id: str,
        *,
        order: RecordOrder,
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        where: CompiledFilter | None = None,
        position: RecordPosition | None = None,
    ) -> RecordsPage:
        """
        Read records in explicit `(timestamp, id)` key order.

        `position` is exclusive: reading continues strictly after that key in
        the requested direction. A finite `limit` fetches one lookahead row to
        decide whether `next_position` (the last returned key) is set;
        unlimited reads return every matching row and never continue.
        """
        order = RecordOrder(order)
        if limit is not None and (type(limit) is not int or limit < 1):
            msg = "Page size must be positive"
            raise ValueError(msg)
        await self._wait_until_available()
        record_type = self._record_types.get(record_type_id)
        if record_type is None:
            return RecordsPage([], None)
        conn = self._require_conn()
        where_sql, params = _build_where_clause(start, end, where)
        ts_col = quote_identifier(COL_TIMESTAMP)
        id_col = quote_identifier(COL_ID)
        if position is not None:
            where_sql += " AND " if where_sql else " WHERE "
            comparison = "<" if order is RecordOrder.DESC else ">"
            where_sql += f"({ts_col}, {id_col}) {comparison} (?, ?)"
            params.extend((position.timestamp_micros, position.record_id))
        table = quote_identifier(record_type.sql_table)
        direction = order.value.upper()
        sql = (
            f"SELECT * FROM {table}{where_sql} "  # noqa: S608
            f"ORDER BY {ts_col} {direction}, {id_col} {direction}"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit + 1)

        def _query() -> list[sqlite3.Row]:
            return conn.execute(sql, params).fetchall()

        rows = await self._run(_query)
        next_position = None
        if limit is not None and len(rows) > limit:
            last = rows[limit - 1]
            next_position = RecordPosition(last[COL_TIMESTAMP], last[COL_ID])
        return RecordsPage(
            [self._row_to_envelope(record_type, row) for row in rows[:limit]],
            next_position,
        )

    async def async_delete_record(self, record_type_id: str, record_id: str) -> bool:
        """Delete a single record by id. Returns True if a record was removed."""
        await self._wait_until_available()
        record_type = self._record_types.get(record_type_id)
        if record_type is None:
            return False
        conn = self._require_conn()

        def _delete() -> bool:
            table = quote_identifier(record_type.sql_table)
            with _transaction(conn):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {quote_identifier(COL_ID)} = ?",  # noqa: S608
                    (record_id,),
                )
            return cursor.rowcount > 0

        deleted = await self._run(_delete)
        if deleted:
            self._fire_updated(record_type_id)
        return deleted

    async def async_purge_expired(
        self, retention_by_type: dict[str, int | None]
    ) -> dict[str, int]:
        """
        Remove records older than retention_days per record type.

        A retention_days of None means "keep forever" (no purge for that type).
        Returns a dict of record_type_id -> number of records removed.
        """
        now = dt_util.utcnow()
        removed_counts: dict[str, int] = {}
        for record_type_id, retention_days in retention_by_type.items():
            await self._wait_until_available()
            record_type = self._record_types.get(record_type_id)
            if record_type is None:
                removed_counts[record_type_id] = 0
                continue
            if retention_days is None:
                removed_counts[record_type_id] = 0
                continue
            if retention_days < 1:
                LOGGER.warning(
                    "Ignoring invalid retention_days %s for record type %s",
                    retention_days,
                    record_type_id,
                )
                removed_counts[record_type_id] = 0
                continue
            cutoff = to_epoch_micros(now - timedelta(days=retention_days))
            conn = self._require_conn()

            def _purge(
                conn: sqlite3.Connection = conn,
                table: str = record_type.sql_table,
                cutoff: int = cutoff,
            ) -> int:
                table_sql = quote_identifier(table)
                with _transaction(conn):
                    cursor = conn.execute(
                        f"DELETE FROM {table_sql} "  # noqa: S608
                        f"WHERE {quote_identifier(COL_TIMESTAMP)} < ?",
                        (cutoff,),
                    )
                return cursor.rowcount

            removed = await self._run(_purge)
            removed_counts[record_type_id] = removed
            if removed:
                self._fire_updated(record_type_id)
        return removed_counts

    async def async_enforce_max_records(
        self, max_records_by_type: dict[str, int | None]
    ) -> dict[str, int]:
        """
        Drop oldest records beyond an optional per-type count cap.

        A max_records of None means "unlimited" (opt-in feature, off by default).
        Returns a dict of record_type_id -> number of records removed.
        """
        removed_counts: dict[str, int] = {}
        for record_type_id, max_records in max_records_by_type.items():
            await self._wait_until_available()
            record_type = self._record_types.get(record_type_id)
            if record_type is None:
                removed_counts[record_type_id] = 0
                continue
            if max_records is None:
                removed_counts[record_type_id] = 0
                continue
            if max_records < 1:
                LOGGER.warning(
                    "Ignoring invalid max_records %s for record type %s",
                    max_records,
                    record_type_id,
                )
                removed_counts[record_type_id] = 0
                continue
            conn = self._require_conn()

            def _enforce(
                conn: sqlite3.Connection = conn,
                table: str = record_type.sql_table,
                limit: int = max_records,
            ) -> int:
                table_sql = quote_identifier(table)
                ts_col = quote_identifier(COL_TIMESTAMP)
                id_col = quote_identifier(COL_ID)
                with _transaction(conn):
                    cursor = conn.execute(
                        f"DELETE FROM {table_sql} WHERE {id_col} IN ("  # noqa: S608
                        f"SELECT {id_col} FROM {table_sql} "
                        f"ORDER BY {ts_col} DESC, {id_col} DESC "
                        "LIMIT -1 OFFSET ?)",
                        (limit,),
                    )
                return cursor.rowcount

            removed = await self._run(_enforce)
            removed_counts[record_type_id] = removed
            if removed:
                self._fire_updated(record_type_id)
        return removed_counts

    async def async_import_records(
        self, record_type_id: str, rows: list[ImportRow]
    ) -> ImportSummary:
        """
        Bulk-import parsed CSV rows (csv_transfer.py) in one transaction.

        Duplicate handling matches the pre-SQL implementation exactly (see
        plan_sql.md Phase 2 pt.15): a row whose `id` already exists is
        skipped (never overwritten); a row without an `id` but with the SAME
        timestamp+field data as an existing row (or an earlier row in this
        same import) is also skipped; a row with no timestamp at all is
        always appended (nothing meaningful to compare).
        """
        await self._wait_until_available()
        record_type = self._record_types.get(record_type_id)
        if record_type is None:
            return ImportSummary(imported=0, skipped_duplicate=0)
        conn = self._require_conn()
        table = quote_identifier(record_type.sql_table)
        ts_col = quote_identifier(COL_TIMESTAMP)
        id_col = quote_identifier(COL_ID)

        def _do_import() -> ImportSummary:
            with _transaction(conn):
                imported = 0
                skipped = 0
                seen_signatures: set[tuple[int, Any]] = set()
                for row in rows:
                    if row.id is not None:
                        dup_id_sql = f"SELECT 1 FROM {table} WHERE {id_col} = ?"  # noqa: S608
                        existing = conn.execute(dup_id_sql, (row.id,)).fetchone()
                        if existing is not None:
                            skipped += 1
                            continue

                    ts = (
                        dt_util.as_utc(row.timestamp)
                        if row.timestamp is not None
                        else dt_util.utcnow()
                    )
                    ts_micros = to_epoch_micros(ts)

                    if row.timestamp is not None:
                        signature = (ts_micros, _freeze(row.fields))
                        if signature in seen_signatures:
                            skipped += 1
                            continue
                        candidates_sql = f"SELECT * FROM {table} WHERE {ts_col} = ?"  # noqa: S608
                        candidates = conn.execute(
                            candidates_sql, (ts_micros,)
                        ).fetchall()
                        is_duplicate = any(
                            _freeze(
                                {
                                    f.key: decode_field(f, candidate[f.sql_column])
                                    for f in record_type.fields
                                    if candidate[f.sql_column] is not None
                                }
                            )
                            == _freeze(row.fields)
                            for candidate in candidates
                        )
                        if is_duplicate:
                            skipped += 1
                            continue
                        seen_signatures.add(signature)

                    record_id = row.id or str(uuid4())
                    columns = [COL_ID, COL_TIMESTAMP]
                    values: list[Any] = [record_id, ts_micros]
                    for field_def in record_type.fields:
                        if field_def.key not in row.fields:
                            continue
                        columns.append(field_def.sql_column)
                        values.append(
                            encode_field(field_def, row.fields[field_def.key])
                        )
                    col_sql = ", ".join(quote_identifier(c) for c in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    conn.execute(
                        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",  # noqa: S608
                        values,
                    )
                    imported += 1
            return ImportSummary(imported=imported, skipped_duplicate=skipped)

        summary = await self._run(_do_import)
        if summary.imported:
            self._fire_updated(record_type_id)
        return summary

    async def async_remove_record_type(self, record_type_id: str) -> None:
        """Drop a record type's table (e.g. its config subentry was removed)."""
        await self._wait_until_available()
        record_type = self._record_types.pop(record_type_id, None)
        if record_type is None:
            return
        conn = self._require_conn()

        def _drop() -> None:
            table = quote_identifier(record_type.sql_table)
            with _transaction(conn):
                conn.execute(f"DROP TABLE IF EXISTS {table}")

        await self._run(_drop)

    # -- aggregation (plan_sql.md Phase 4 + group_by/metrics/cumulative follow-up) --

    async def async_aggregate_records(  # noqa: PLR0913 (one param per query dimension)
        self,
        record_type_id: str,
        metrics: list[MetricSpec],
        bucket: AggregateBucket | int | None,
        group_by_field_key: str | None,
        start: datetime | None,
        end: datetime | None,
        where: CompiledFilter | None,
        *,
        cumulative: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return sparse ascending aggregate rows for a record type.

        Each result row is `{"bucket": str | None, "group": Any | None,
        "metrics": {name: {"value":.., "count":..}}}` - wire-format-agnostic;
        `websocket_api.py` reshapes this into the public legacy/new x
        table/apexcharts JSON shapes. `bucket`/`group_by_field_key` being
        `None` means "no time axis"/"no categorical axis" respectively; with
        both `None`, SQL's own no-`GROUP BY` semantics naturally return
        exactly one summary row (even with zero matching records - e.g.
        `SUM` -> `NULL`, `COUNT` -> 0), so no special-casing is needed here.
        """
        await self._wait_until_available()
        record_type = self._record_types.get(record_type_id)
        if record_type is None:
            return []
        conn = self._require_conn()

        if cumulative and bucket is None:
            msg = "'cumulative' requires 'bucket' to be set"
            raise ValueError(msg)

        where_sql, params = _build_where_clause(start, end, where)
        sql, group_cols, group_field_def = _build_aggregate_sql(
            record_type,
            metrics,
            bucket,
            group_by_field_key,
            where_sql,
            cumulative=cumulative,
        )

        def _query() -> list[sqlite3.Row]:
            return conn.execute(sql, params).fetchall()

        rows = await self._run(_query)
        return [
            _aggregate_row_to_result(row, metrics, group_cols, group_field_def)
            for row in rows
        ]

    async def async_field_stats(  # noqa: PLR0913 (one param per query dimension)
        self,
        record_type_id: str,
        field_key: str,
        start: datetime | None,
        end: datetime | None,
        where: CompiledFilter | None,
        want: set[str],
    ) -> dict[str, Any]:
        """Return first/last/min/max/sum/avg/count for a numeric field."""
        await self._wait_until_available()
        record_type = self._record_types.get(record_type_id)
        if record_type is None:
            return {}
        field_def = record_type.get_field(field_key)
        if field_def is None:
            msg = f"Unknown field '{field_key}'"
            raise ValueError(msg)
        conn = self._require_conn()
        col = quote_identifier(field_def.sql_column)
        table = quote_identifier(record_type.sql_table)
        where_sql, params = _build_where_clause(start, end, where)
        ts_col = quote_identifier(COL_TIMESTAMP)
        id_col = quote_identifier(COL_ID)

        result: dict[str, Any] = {}

        if want & {"min", "max", "sum", "avg", "count"}:
            # col/table are validated+quoted; only ? params are bound below.
            agg_sql = (
                f"SELECT MIN({col}) AS mn, MAX({col}) AS mx, SUM({col}) AS sm, "  # noqa: S608
                f"AVG({col}) AS av, COUNT({col}) AS cnt FROM {table}{where_sql}"
            )

            def _query_agg() -> sqlite3.Row:
                return conn.execute(agg_sql, params).fetchone()

            row = await self._run(_query_agg)
            for value in (row["sm"], row["av"]):
                if isinstance(value, float) and not is_finite_number(value):
                    msg = "Aggregate result is not a finite number"
                    raise ValueError(msg)
            if "min" in want:
                result["min"] = row["mn"]
            if "max" in want:
                result["max"] = row["mx"]
            if "sum" in want:
                result["sum"] = row["sm"]
            if "avg" in want:
                result["avg"] = row["av"]
            if "count" in want:
                result["count"] = row["cnt"]

        if "first" in want:
            # col/table are validated+quoted; only ? params are bound below.
            first_sql = (
                f"SELECT {col} AS v FROM {table}{where_sql} "  # noqa: S608
                f"ORDER BY {ts_col} ASC, {id_col} ASC LIMIT 1"
            )

            def _query_first() -> sqlite3.Row | None:
                return conn.execute(first_sql, params).fetchone()

            row = await self._run(_query_first)
            result["first"] = row["v"] if row is not None else None

        if "last" in want:
            # col/table are validated+quoted; only ? params are bound below.
            last_sql = (
                f"SELECT {col} AS v FROM {table}{where_sql} "  # noqa: S608
                f"ORDER BY {ts_col} DESC, {id_col} DESC LIMIT 1"
            )

            def _query_last() -> sqlite3.Row | None:
                return conn.execute(last_sql, params).fetchone()

            row = await self._run(_query_last)
            result["last"] = row["v"] if row is not None else None

        return result

    async def async_histogram_records(  # noqa: PLR0913 (one param per query dimension)
        self,
        record_type_id: str,
        field_key: str,
        start: datetime | None,
        end: datetime | None,
        where: CompiledFilter | None,
        bin_count: int | None,
        bin_width: float | None,
        min_override: float | None,
        max_override: float | None,
    ) -> dict[str, Any]:
        """Return value-distribution bins for a numeric field."""
        await self._wait_until_available()
        _validate_histogram_bounds(min_override, max_override)
        record_type = self._record_types.get(record_type_id)
        if record_type is None:
            return {"bins": [], "min": None, "max": None, "bin_width": None}
        field_def = record_type.get_field(field_key)
        if field_def is None:
            msg = f"Unknown field '{field_key}'"
            raise ValueError(msg)
        conn = self._require_conn()
        col = quote_identifier(field_def.sql_column)
        table = quote_identifier(record_type.sql_table)
        where_sql, params = _build_where_clause(start, end, where)
        not_null_sql = f"{col} IS NOT NULL"
        combined_where_sql = (
            f"{where_sql} AND {not_null_sql}" if where_sql else f" WHERE {not_null_sql}"
        )

        data_min = min_override
        data_max = max_override
        if data_min is None or data_max is None:
            range_where_sql, range_params = _add_histogram_bounds(
                combined_where_sql, params, col, min_override, max_override
            )
            # col/table are validated+quoted; only ? params are bound below.
            range_sql = (
                f"SELECT MIN({col}) AS mn, MAX({col}) AS mx "  # noqa: S608
                f"FROM {table}{range_where_sql}"
            )

            def _query_range() -> sqlite3.Row:
                return conn.execute(range_sql, range_params).fetchone()

            row = await self._run(_query_range)
            if data_min is None:
                data_min = row["mn"]
            if data_max is None:
                data_max = row["mx"]

        if data_min is None or data_max is None:
            return {"bins": [], "min": None, "max": None, "bin_width": None}

        if data_min == data_max:
            resolved_bin_count = 1
            width = 1.0
        elif bin_width is not None:
            width = bin_width
            resolved_bin_count = max(1, ceil((data_max - data_min) / width))
        else:
            resolved_bin_count = bin_count or DEFAULT_HISTOGRAM_BIN_COUNT
            width = (data_max - data_min) / resolved_bin_count

        if resolved_bin_count > MAX_HISTOGRAM_BINS:
            msg = (
                f"Resolved bin count {resolved_bin_count} exceeds the maximum "
                f"of {MAX_HISTOGRAM_BINS}; use a larger bin_width"
            )
            raise ValueError(msg)

        # SQLite's 2-arg MIN()/MAX() are scalar (not aggregate) - clamps the
        # top bin so a value exactly at data_max lands in the last bin
        # instead of spilling into a phantom extra bin. col/table are
        # validated+quoted; only ? params are bound below.
        bounded_where_sql, bounded_params = _add_histogram_bounds(
            combined_where_sql, params, col, data_min, data_max
        )
        bin_sql = (
            f"SELECT MIN(CAST(({col} - ?) / ? AS INTEGER), ?) AS bin, "  # noqa: S608
            f"COUNT(*) AS n FROM {table}{bounded_where_sql} GROUP BY bin"
        )
        # Placeholder order must match their physical order in `bin_sql`'s
        # text: the 3 SELECT-clause placeholders come first, THEN the WHERE
        # clause's own `params` (from `combined_where_sql`).
        bin_params = [
            data_min,
            width,
            resolved_bin_count - 1,
            *bounded_params,
        ]

        def _query_bins() -> list[sqlite3.Row]:
            return conn.execute(bin_sql, bin_params).fetchall()

        rows = await self._run(_query_bins)
        counts = dict.fromkeys(range(resolved_bin_count), 0)
        for row in rows:
            counts[row["bin"]] = row["n"]

        bins = [
            {
                "range_start": data_min + i * width,
                "range_end": data_min + (i + 1) * width,
                "count": counts[i],
            }
            for i in range(resolved_bin_count)
        ]
        return {"bins": bins, "min": data_min, "max": data_max, "bin_width": width}

    # -- lifecycle --------------------------------------------------------------

    async def async_close(self) -> None:
        """Commit/close the connection and shut down this entry's DB worker."""
        self._closed = True
        self._database_available.set()
        conn = self._conn
        self._conn = None
        if conn is not None:
            await self._run_direct(conn.close)
        self._executor.shutdown(wait=False)

    async def async_prepare_backup(self) -> None:
        """Pause new operations, drain queued work, and close the database."""
        if self._backup_in_progress:
            return
        self._backup_in_progress = True
        self._database_available.clear()
        try:
            conn = self._conn
            self._conn = None
            if conn is not None:
                await self._run_direct(conn.close)
        except BaseException:
            self._backup_in_progress = False
            self._database_available.set()
            raise

    async def async_finish_backup(self) -> None:
        """Reopen and validate the database, then resume normal operations."""
        if not self._backup_in_progress:
            return
        conn: sqlite3.Connection | None = None
        try:
            conn, _ = await self._run_direct(_open_sync, self._db_path)
            for record_type in self._record_types.values():
                await self._run_direct(_validate_table_sync, conn, record_type)
            self._conn = conn
        except BaseException:
            if conn is not None:
                await self._run_direct(conn.close)
            raise
        finally:
            self._backup_in_progress = False
            self._database_available.set()

    async def async_remove(self) -> None:
        """Close (if open) and permanently delete this entry's database file."""
        await self.async_close()

        def _delete_files() -> None:
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(f"{self._db_path}{suffix}").unlink(missing_ok=True)

        await self.hass.async_add_executor_job(_delete_files)
