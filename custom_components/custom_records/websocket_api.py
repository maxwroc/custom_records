"""WebSocket API commands used by the custom Lovelace card."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.decorators import (
    async_response,
    websocket_command,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from .const import (
    ALL_FIELD_STATS,
    ATTR_BIN_COUNT,
    ATTR_BIN_WIDTH,
    ATTR_BUCKET,
    ATTR_CUMULATIVE,
    ATTR_CURRENT,
    ATTR_END,
    ATTR_FIELD,
    ATTR_FIELDS,
    ATTR_FILTER,
    ATTR_FORMAT,
    ATTR_GROUP_BY,
    ATTR_LIMIT,
    ATTR_MAX,
    ATTR_METRICS,
    ATTR_MIN,
    ATTR_OP,
    ATTR_PREVIOUS,
    ATTR_RECORD_TYPE,
    ATTR_START,
    ATTR_STATS,
    ATTR_TIMESTAMP,
    AUTO_BUCKET_DAY_MAX_DAYS,
    AUTO_BUCKET_HOUR_MAX_DAYS,
    AUTO_BUCKET_WEEK_MAX_DAYS,
    CUSTOM_BUCKET_PATTERN,
    DOMAIN,
    MAX_LIST_RECORDS_LIMIT,
    MAX_METRICS_PER_CALL,
    NUMERIC_AGGREGATE_OPS,
    AggregateBucket,
    AggregateFormat,
    AggregateOp,
    FieldType,
    RecordOrder,
)
from .filter_query import FilterError, compile_record_filter
from .media_store import ImageStoreError, async_validate_image_path
from .pagination import CursorError, query_scope
from .record_view import to_public_record
from .schema import validate_record_data
from .sql_encoding import is_finite_number
from .store import MetricSpec

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.components.websocket_api.connection import ActiveConnection
    from homeassistant.core import HomeAssistant

    from .models import RecordType
    from .runtime_data import CustomRecordsRuntimeData

_WS_REGISTERED_KEY = f"{DOMAIN}_ws_registered"


def _parse_datetime(value: str) -> datetime:
    """Parse an API datetime value, raising ValueError when malformed/naive."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        msg = f"Invalid datetime '{value}'"
        raise ValueError(msg)
    if parsed.tzinfo is None:
        msg = f"Datetime '{value}' must include a UTC offset"
        raise ValueError(msg)
    return parsed


def _get_runtime_data(hass: HomeAssistant) -> CustomRecordsRuntimeData | None:
    """Return the runtime data for the (single) loaded config entry, if any."""
    entries = hass.config_entries.async_entries(DOMAIN)
    loaded = [entry for entry in entries if entry.state is ConfigEntryState.LOADED]
    return loaded[0].runtime_data if loaded else None


@websocket_command({vol.Required("type"): "custom_records/list_record_types"})
@async_response
async def handle_list_record_types(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return all configured record types."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    connection.send_result(
        msg["id"],
        {"record_types": [rt.to_dict() for rt in runtime_data.record_types.values()]},
    )


@websocket_command(
    {
        vol.Required("type"): "custom_records/list_records",
        vol.Required(ATTR_RECORD_TYPE): str,
        vol.Optional("start"): str,
        vol.Optional("end"): str,
        vol.Optional(ATTR_LIMIT): vol.All(int, vol.Range(min=1)),
        vol.Optional(ATTR_FILTER): list,
        vol.Optional("paginate", default=False): bool,
        vol.Optional("order", default=RecordOrder.DESC.value): vol.Coerce(RecordOrder),
        vol.Optional("cursor"): object,
    }
)
@async_response
async def handle_list_records(  # noqa: PLR0911 (independent validation errors)
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return records for a record type, optionally filtered by time range."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    record_type_id = msg[ATTR_RECORD_TYPE]
    record_type = runtime_data.record_types.get(record_type_id)
    if record_type is None:
        connection.send_error(
            msg["id"], "unknown_record_type", f"Unknown record_type '{record_type_id}'"
        )
        return
    if "cursor" in msg and not msg["paginate"]:
        connection.send_error(
            msg["id"], "invalid_cursor", "Cursor requires paginate: true"
        )
        return
    try:
        where = compile_record_filter(record_type, msg.get(ATTR_FILTER))
    except FilterError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    try:
        start = _parse_datetime(msg["start"]) if "start" in msg else None
        end = _parse_datetime(msg["end"]) if "end" in msg else None
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_datetime", str(err))
        return
    if start is not None and end is not None and start > end:
        connection.send_error(
            msg["id"], "invalid_time_range", "Start datetime must not be after end"
        )
        return
    # Always apply a server-side cap, regardless of what the caller requests,
    # so response payload size stays bounded as a record type grows.
    if ATTR_LIMIT in msg:
        limit = min(msg[ATTR_LIMIT], MAX_LIST_RECORDS_LIMIT)
    else:
        limit = MAX_LIST_RECORDS_LIMIT
    order: RecordOrder = msg["order"]
    scope = query_scope(
        runtime_data.storage.entry_id, record_type_id, start, end, where, order
    )
    try:
        position = (
            runtime_data.cursors.resolve(msg["cursor"], scope)
            if "cursor" in msg
            else None
        )
        page = await runtime_data.storage.async_list_records(
            record_type_id,
            order=order,
            start=start,
            end=end,
            limit=limit,
            where=where,
            position=position,
        )
        result: dict[str, Any] = {
            "records": [to_public_record(r, record_type) for r in page.records]
        }
        if msg["paginate"]:
            cursor = (
                runtime_data.cursors.issue(scope, page.next_position)
                if page.next_position is not None
                else None
            )
            result.update(has_more=cursor is not None, next_cursor=cursor)
    except CursorError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], result)


def _validate_aggregate_field(
    record_type: RecordType, op: AggregateOp, field_key: str | None
) -> tuple[str, str] | None:
    """Return (error_code, message) if op/field don't match, else None."""
    if op in NUMERIC_AGGREGATE_OPS:
        if field_key is None:
            return "field_required", f"'field' is required for op '{op}'"
        field_def = record_type.get_field(field_key)
        if field_def is None:
            return "unknown_field", f"Unknown field '{field_key}'"
        if field_def.type is not FieldType.NUMBER:
            return "unsupported_field", f"Field '{field_key}' is not a number field"
        return None
    if field_key is not None:
        return "field_forbidden", f"'field' is not allowed for op '{op}'"
    return None


class AggregateRequestError(Exception):
    """Raised for `aggregate_records` request-shape validation problems."""

    def __init__(self, code: str, message: str) -> None:
        """Store a machine-readable error code alongside the message."""
        super().__init__(message)
        self.code = code
        self.message = message


# Machine-readable AggregateRequestError codes (named constants, not inline
# string literals, matching filter_query.py's ERR_* convention).
_ERR_INVALID_BUCKET = "invalid_bucket"
_ERR_INVALID_METRICS = "invalid_metrics"
_ERR_TOO_MANY_METRICS = "too_many_metrics"
_ERR_DUPLICATE_METRIC_NAME = "duplicate_metric_name"

# Internal-only metric name used for the legacy singular op/field call shape
# - discarded during response reshaping, never exposed to callers.
_LEGACY_METRIC_NAME = "_value"


def _resolve_bucket(  # noqa: PLR0911 (many independent early-return cases)
    raw: str | None, start: datetime | None, end: datetime | None
) -> AggregateBucket | int | None:
    """
    Parse the `bucket` param into a calendar bucket, fixed seconds, or None.

    `None` (bucket omitted) means "no time axis". `"auto"` picks a named
    calendar bucket from the `start`/`end` span. A duration string like
    `"15m"`/`"2h"` (reusing the card's existing duration convention) is a
    custom fixed-size, epoch-aligned bucket - minutes/hours only, since
    day/week+ granularity must stay calendar-aware via the named buckets.
    """
    if raw is None:
        return None
    if raw == "auto":
        if start is None or end is None:
            msg = "'bucket: auto' requires both 'start' and 'end'"
            raise AggregateRequestError(_ERR_INVALID_BUCKET, msg)
        span_days = (end - start).total_seconds() / 86400
        if span_days <= AUTO_BUCKET_HOUR_MAX_DAYS:
            return AggregateBucket.HOUR
        if span_days <= AUTO_BUCKET_DAY_MAX_DAYS:
            return AggregateBucket.DAY
        if span_days <= AUTO_BUCKET_WEEK_MAX_DAYS:
            return AggregateBucket.WEEK
        return AggregateBucket.MONTH
    custom_match = CUSTOM_BUCKET_PATTERN.fullmatch(raw)
    if custom_match:
        count = int(custom_match.group(1))
        seconds = count * (60 if custom_match.group(2).lower() == "m" else 3600)
        if seconds <= 0:
            msg = f"Invalid bucket duration '{raw}'"
            raise AggregateRequestError(_ERR_INVALID_BUCKET, msg)
        return seconds
    try:
        return AggregateBucket(raw)
    except ValueError as err:
        msg = f"Invalid bucket '{raw}'"
        raise AggregateRequestError(_ERR_INVALID_BUCKET, msg) from err


def _parse_metrics(record_type: RecordType, raw_metrics: list[Any]) -> list[MetricSpec]:
    """Validate and build the `MetricSpec` list from the new `metrics` param."""
    if len(raw_metrics) == 0:
        msg = "'metrics' must contain at least one entry"
        raise AggregateRequestError(_ERR_INVALID_METRICS, msg)
    if len(raw_metrics) > MAX_METRICS_PER_CALL:
        msg = f"'metrics' may not contain more than {MAX_METRICS_PER_CALL} entries"
        raise AggregateRequestError(_ERR_TOO_MANY_METRICS, msg)
    metrics: list[MetricSpec] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw_metrics):
        if not isinstance(item, dict) or "op" not in item:
            msg = f"'metrics[{i}]' must be an object with at least 'op'"
            raise AggregateRequestError(_ERR_INVALID_METRICS, msg)
        try:
            op = AggregateOp(item["op"])
        except ValueError as err:
            msg = f"'metrics[{i}].op' is invalid"
            raise AggregateRequestError(_ERR_INVALID_METRICS, msg) from err
        field_key = item.get("field")
        field_error = _validate_aggregate_field(record_type, op, field_key)
        if field_error is not None:
            raise AggregateRequestError(*field_error)
        raw_name = item.get("name")
        if raw_name is not None and (
            not isinstance(raw_name, str) or not raw_name.strip()
        ):
            msg = f"'metrics[{i}].name' must be a non-empty string"
            raise AggregateRequestError(_ERR_INVALID_METRICS, msg)
        name = raw_name or f"{op.value}_{field_key or 'count'}"
        if name in seen_names:
            msg = f"Duplicate metric name '{name}'"
            raise AggregateRequestError(_ERR_DUPLICATE_METRIC_NAME, msg)
        seen_names.add(name)
        metrics.append(MetricSpec(op=op, field_key=field_key, name=name))
    return metrics


def _bucket_to_epoch_ms(bucket_str: str) -> int | None:
    """Convert one bucket-start ISO label into epoch milliseconds."""
    parsed = dt_util.parse_datetime(bucket_str)
    return None if parsed is None else int(parsed.timestamp() * 1000)


def _stringify_group(value: Any) -> str:
    """Render a group value as a string for apexcharts series name/label use."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _group_identity(value: Any) -> Any:
    """Return a hashable identity for a decoded group value."""
    if isinstance(value, dict):
        return tuple(
            (key, _group_identity(item)) for key, item in sorted(value.items())
        )
    if isinstance(value, list):
        return tuple(_group_identity(item) for item in value)
    return value


def _row_value_count(row: dict[str, Any], name: str) -> tuple[Any, int]:
    """Return (value, count) for one named metric within a store result row."""
    metric = row["metrics"][name]
    return metric["value"], metric["count"]


def _shape_legacy_result(  # noqa: PLR0911, PLR0913 (many shapes, one param per dimension)
    rows: list[dict[str, Any]],
    metric: MetricSpec,
    record_type: RecordType,
    fmt: AggregateFormat,
    *,
    has_bucket: bool,
    has_group: bool,
) -> dict[str, Any]:
    """Reshape store rows into the pre-existing (legacy singular op/field) shape."""
    if fmt is AggregateFormat.TABLE:
        if has_bucket and has_group:
            buckets = [
                {
                    "start": row["bucket"],
                    "group": row["group"],
                    "value": (vc := _row_value_count(row, metric.name))[0],
                    "count": vc[1],
                }
                for row in rows
            ]
            return {"buckets": buckets}
        if has_bucket:
            buckets = [
                {"start": row["bucket"], "value": v, "count": c}
                for row in rows
                for v, c in [_row_value_count(row, metric.name)]
            ]
            return {"buckets": buckets}
        if has_group:
            groups = [
                {"group": row["group"], "value": v, "count": c}
                for row in rows
                for v, c in [_row_value_count(row, metric.name)]
            ]
            return {"groups": groups}
        value, count = _row_value_count(rows[0], metric.name) if rows else (None, 0)
        return {"value": value, "count": count}

    # apexcharts
    if has_bucket and has_group:
        series_map: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            x = _bucket_to_epoch_ms(row["bucket"])
            if x is None:
                continue
            value, _count = _row_value_count(row, metric.name)
            name = _stringify_group(row["group"])
            series_map.setdefault(name, []).append({"x": x, "y": value})
        return {
            "series": [
                {"name": name, "data": data} for name, data in series_map.items()
            ]
        }
    if has_bucket:
        series_name = "Count"
        if metric.field_key is not None:
            field_def = record_type.get_field(metric.field_key)
            if field_def is not None:
                series_name = field_def.label
        data = []
        for row in rows:
            x = _bucket_to_epoch_ms(row["bucket"])
            if x is None:
                continue
            value, _count = _row_value_count(row, metric.name)
            data.append({"x": x, "y": value})
        return {"series": [{"name": series_name, "data": data}]}
    # has_group only (categorical - pie/bar friendly)
    labels = [_stringify_group(row["group"]) for row in rows]
    series = [_row_value_count(row, metric.name)[0] for row in rows]
    return {"labels": labels, "series": series}


def _shape_metrics_result(
    rows: list[dict[str, Any]],
    metrics: list[MetricSpec],
    fmt: AggregateFormat,
    *,
    has_bucket: bool,
) -> dict[str, Any]:
    """Reshape store rows into the new multi-metric (`metrics` param) shape."""
    names = [m.name for m in metrics]
    if fmt is AggregateFormat.TABLE:
        if has_bucket:
            buckets = [
                {
                    "start": row["bucket"],
                    "values": {n: _row_value_count(row, n)[0] for n in names},
                    "counts": {n: _row_value_count(row, n)[1] for n in names},
                }
                for row in rows
            ]
            return {"buckets": buckets}
        if rows:
            row = rows[0]
            return {
                "values": {n: _row_value_count(row, n)[0] for n in names},
                "counts": {n: _row_value_count(row, n)[1] for n in names},
            }
        return {"values": dict.fromkeys(names), "counts": dict.fromkeys(names, 0)}

    # apexcharts - always has a bucket here (format-compatibility already
    # enforced group_by-free 'metrics' calls to have `bucket` set).
    series = []
    for name in names:
        data = []
        for row in rows:
            x = _bucket_to_epoch_ms(row["bucket"])
            if x is None:
                continue
            data.append({"x": x, "y": _row_value_count(row, name)[0]})
        series.append({"name": name, "data": data})
    return {"series": series}


@websocket_command(
    {
        vol.Required("type"): "custom_records/aggregate_records",
        vol.Required(ATTR_RECORD_TYPE): str,
        vol.Optional(ATTR_OP): vol.Coerce(AggregateOp),
        vol.Optional(ATTR_BUCKET): str,
        vol.Optional(ATTR_FIELD): str,
        vol.Optional(ATTR_START): str,
        vol.Optional(ATTR_END): str,
        vol.Optional(ATTR_FORMAT, default=AggregateFormat.TABLE.value): vol.Coerce(
            AggregateFormat
        ),
        vol.Optional(ATTR_FILTER): list,
        vol.Optional(ATTR_GROUP_BY): str,
        vol.Optional(ATTR_METRICS): list,
        vol.Optional(ATTR_CUMULATIVE, default=False): bool,
    }
)
@async_response
async def handle_aggregate_records(  # noqa: PLR0911, PLR0912, PLR0915 (many independent validations)
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return structured aggregate data: buckets, groups, and/or multi-metric series."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    record_type_id = msg[ATTR_RECORD_TYPE]
    record_type = runtime_data.record_types.get(record_type_id)
    if record_type is None:
        connection.send_error(
            msg["id"], "unknown_record_type", f"Unknown record_type '{record_type_id}'"
        )
        return

    try:
        where = compile_record_filter(record_type, msg.get(ATTR_FILTER))
    except FilterError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    try:
        start = _parse_datetime(msg["start"]) if "start" in msg else None
        end = _parse_datetime(msg["end"]) if "end" in msg else None
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_datetime", str(err))
        return
    if start is not None and end is not None and start > end:
        connection.send_error(
            msg["id"], "invalid_time_range", "Start datetime must not be after end"
        )
        return

    has_metrics_param = ATTR_METRICS in msg
    has_legacy_op = ATTR_OP in msg
    if has_metrics_param and has_legacy_op:
        connection.send_error(
            msg["id"],
            "legacy_metrics_conflict",
            "Use either 'op'/'field' or 'metrics', not both",
        )
        return
    if not has_metrics_param and not has_legacy_op:
        connection.send_error(
            msg["id"], "op_or_metrics_required", "Either 'op' or 'metrics' is required"
        )
        return

    group_by_key = msg.get(ATTR_GROUP_BY)
    if group_by_key is not None and has_metrics_param:
        connection.send_error(
            msg["id"],
            "group_by_metrics_conflict",
            "'group_by' cannot be combined with 'metrics'; use 'op'/'field' with "
            "'group_by' instead",
        )
        return
    if group_by_key is not None and record_type.get_field(group_by_key) is None:
        connection.send_error(
            msg["id"], "unknown_group_by_field", f"Unknown field '{group_by_key}'"
        )
        return

    try:
        if has_metrics_param:
            metrics = _parse_metrics(record_type, msg[ATTR_METRICS])
        else:
            op: AggregateOp = msg[ATTR_OP]
            field_key = msg.get(ATTR_FIELD)
            field_error = _validate_aggregate_field(record_type, op, field_key)
            if field_error is not None:
                raise AggregateRequestError(*field_error)  # noqa: TRY301
            metrics = [MetricSpec(op=op, field_key=field_key, name=_LEGACY_METRIC_NAME)]
        bucket = _resolve_bucket(msg.get(ATTR_BUCKET), start, end)
    except AggregateRequestError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return

    cumulative = msg[ATTR_CUMULATIVE]
    if cumulative and bucket is None:
        connection.send_error(
            msg["id"],
            "cumulative_requires_bucket",
            "'cumulative' requires 'bucket' to be set",
        )
        return

    fmt = msg[ATTR_FORMAT]
    if fmt is AggregateFormat.APEXCHARTS and bucket is None and group_by_key is None:
        connection.send_error(
            msg["id"],
            "unsupported_format",
            "'format: apexcharts' requires 'bucket' and/or 'group_by'",
        )
        return

    try:
        rows = await runtime_data.storage.async_aggregate_records(
            record_type_id,
            metrics,
            bucket,
            group_by_key,
            start,
            end,
            where,
            cumulative=cumulative,
        )
    except ValueError as err:
        connection.send_error(msg["id"], "aggregation_error", str(err))
        return

    has_bucket = bucket is not None
    has_group = group_by_key is not None
    if has_metrics_param:
        result = _shape_metrics_result(rows, metrics, fmt, has_bucket=has_bucket)
    else:
        result = _shape_legacy_result(
            rows,
            metrics[0],
            record_type,
            fmt,
            has_bucket=has_bucket,
            has_group=has_group,
        )
    connection.send_result(msg["id"], result)


@websocket_command(
    {
        vol.Required("type"): "custom_records/get_field_stats",
        vol.Required(ATTR_RECORD_TYPE): str,
        vol.Required(ATTR_FIELD): str,
        vol.Optional(ATTR_START): str,
        vol.Optional(ATTR_END): str,
        vol.Optional(ATTR_FILTER): list,
        vol.Optional(ATTR_STATS): [vol.In(ALL_FIELD_STATS)],
    }
)
@async_response
async def handle_get_field_stats(  # noqa: PLR0911 (many independent early-exit validations)
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return first/last/min/max/sum/avg/count for a numeric field over a period."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    record_type_id = msg[ATTR_RECORD_TYPE]
    record_type = runtime_data.record_types.get(record_type_id)
    if record_type is None:
        connection.send_error(
            msg["id"], "unknown_record_type", f"Unknown record_type '{record_type_id}'"
        )
        return
    field_key = msg[ATTR_FIELD]
    field_def = record_type.get_field(field_key)
    if field_def is None:
        connection.send_error(
            msg["id"], "unknown_field", f"Unknown field '{field_key}'"
        )
        return
    if field_def.type is not FieldType.NUMBER:
        connection.send_error(
            msg["id"], "unsupported_field", f"Field '{field_key}' is not a number field"
        )
        return
    try:
        where = compile_record_filter(record_type, msg.get(ATTR_FILTER))
    except FilterError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    try:
        start = _parse_datetime(msg["start"]) if "start" in msg else None
        end = _parse_datetime(msg["end"]) if "end" in msg else None
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_datetime", str(err))
        return
    if start is not None and end is not None and start > end:
        connection.send_error(
            msg["id"], "invalid_time_range", "Start datetime must not be after end"
        )
        return

    want: set[str] = set(msg.get(ATTR_STATS) or ALL_FIELD_STATS)
    try:
        stats = await runtime_data.storage.async_field_stats(
            record_type_id, field_key, start, end, where, want
        )
    except ValueError as err:
        connection.send_error(msg["id"], "aggregation_error", str(err))
        return
    connection.send_result(msg["id"], {"stats": stats})


@websocket_command(
    {
        vol.Required("type"): "custom_records/histogram_records",
        vol.Required(ATTR_RECORD_TYPE): str,
        vol.Required(ATTR_FIELD): str,
        vol.Optional(ATTR_START): str,
        vol.Optional(ATTR_END): str,
        vol.Optional(ATTR_FILTER): list,
        vol.Optional(ATTR_BIN_COUNT): vol.All(int, vol.Range(min=1)),
        vol.Optional(ATTR_BIN_WIDTH): vol.All(
            vol.Coerce(float), vol.Range(min=0, min_included=False)
        ),
        vol.Optional(ATTR_MIN): vol.Coerce(float),
        vol.Optional(ATTR_MAX): vol.Coerce(float),
    }
)
@async_response
async def handle_histogram_records(  # noqa: PLR0911 (many independent early-exit validations)
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return value-distribution bins for a numeric field."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    record_type_id = msg[ATTR_RECORD_TYPE]
    record_type = runtime_data.record_types.get(record_type_id)
    if record_type is None:
        connection.send_error(
            msg["id"], "unknown_record_type", f"Unknown record_type '{record_type_id}'"
        )
        return
    field_key = msg[ATTR_FIELD]
    field_def = record_type.get_field(field_key)
    if field_def is None:
        connection.send_error(
            msg["id"], "unknown_field", f"Unknown field '{field_key}'"
        )
        return
    if field_def.type is not FieldType.NUMBER:
        connection.send_error(
            msg["id"], "unsupported_field", f"Field '{field_key}' is not a number field"
        )
        return
    if ATTR_BIN_COUNT in msg and ATTR_BIN_WIDTH in msg:
        connection.send_error(
            msg["id"],
            "bin_count_width_conflict",
            "Provide either 'bin_count' or 'bin_width', not both",
        )
        return
    min_override = msg.get(ATTR_MIN)
    max_override = msg.get(ATTR_MAX)
    if any(
        value is not None and not is_finite_number(value)
        for value in (min_override, max_override)
    ):
        connection.send_error(
            msg["id"], "invalid_histogram_range", "Histogram bounds must be finite"
        )
        return
    if (
        min_override is not None
        and max_override is not None
        and min_override >= max_override
    ):
        connection.send_error(
            msg["id"],
            "invalid_histogram_range",
            "Histogram 'min' must be less than 'max'",
        )
        return
    try:
        where = compile_record_filter(record_type, msg.get(ATTR_FILTER))
    except FilterError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    try:
        start = _parse_datetime(msg["start"]) if "start" in msg else None
        end = _parse_datetime(msg["end"]) if "end" in msg else None
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_datetime", str(err))
        return
    if start is not None and end is not None and start > end:
        connection.send_error(
            msg["id"], "invalid_time_range", "Start datetime must not be after end"
        )
        return

    try:
        histogram = await runtime_data.storage.async_histogram_records(
            record_type_id,
            field_key,
            start,
            end,
            where,
            msg.get(ATTR_BIN_COUNT),
            msg.get(ATTR_BIN_WIDTH),
            min_override,
            max_override,
        )
    except ValueError as err:
        connection.send_error(msg["id"], "too_many_bins", str(err))
        return
    connection.send_result(msg["id"], histogram)


_PERIOD_SCHEMA = {vol.Required("start"): str, vol.Required("end"): str}


@websocket_command(
    {
        vol.Required("type"): "custom_records/compare_periods",
        vol.Required(ATTR_RECORD_TYPE): str,
        vol.Required(ATTR_OP): vol.Coerce(AggregateOp),
        vol.Optional(ATTR_FIELD): str,
        vol.Optional(ATTR_FILTER): list,
        vol.Optional(ATTR_GROUP_BY): str,
        vol.Required(ATTR_CURRENT): _PERIOD_SCHEMA,
        vol.Optional(ATTR_PREVIOUS): _PERIOD_SCHEMA,
    }
)
@async_response
async def handle_compare_periods(  # noqa: PLR0911, PLR0912, PLR0915 (many independent validations)
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return current vs. previous period aggregate values and their delta."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    record_type_id = msg[ATTR_RECORD_TYPE]
    record_type = runtime_data.record_types.get(record_type_id)
    if record_type is None:
        connection.send_error(
            msg["id"], "unknown_record_type", f"Unknown record_type '{record_type_id}'"
        )
        return

    op: AggregateOp = msg[ATTR_OP]
    field_key = msg.get(ATTR_FIELD)
    field_error = _validate_aggregate_field(record_type, op, field_key)
    if field_error is not None:
        connection.send_error(msg["id"], *field_error)
        return
    group_by_key = msg.get(ATTR_GROUP_BY)
    if group_by_key is not None and record_type.get_field(group_by_key) is None:
        connection.send_error(
            msg["id"], "unknown_group_by_field", f"Unknown field '{group_by_key}'"
        )
        return
    try:
        where = compile_record_filter(record_type, msg.get(ATTR_FILTER))
    except FilterError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return

    try:
        current_start = _parse_datetime(msg[ATTR_CURRENT]["start"])
        current_end = _parse_datetime(msg[ATTR_CURRENT]["end"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_datetime", str(err))
        return
    if current_start > current_end:
        connection.send_error(
            msg["id"], "invalid_time_range", "Start datetime must not be after end"
        )
        return

    previous_raw = msg.get(ATTR_PREVIOUS)
    if previous_raw is not None:
        try:
            previous_start = _parse_datetime(previous_raw["start"])
            previous_end = _parse_datetime(previous_raw["end"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_datetime", str(err))
            return
        if previous_start > previous_end:
            connection.send_error(
                msg["id"], "invalid_time_range", "Start datetime must not be after end"
            )
            return
    else:
        span = current_end - current_start
        previous_end = current_start - timedelta(microseconds=1)
        previous_start = previous_end - span

    metric = MetricSpec(op=op, field_key=field_key, name=_LEGACY_METRIC_NAME)
    try:
        current_rows = await runtime_data.storage.async_aggregate_records(
            record_type_id,
            [metric],
            None,
            group_by_key,
            current_start,
            current_end,
            where,
        )
        previous_rows = await runtime_data.storage.async_aggregate_records(
            record_type_id,
            [metric],
            None,
            group_by_key,
            previous_start,
            previous_end,
            where,
        )
    except ValueError as err:
        connection.send_error(msg["id"], "aggregation_error", str(err))
        return

    if group_by_key is None:
        cur_value, cur_count = (
            _row_value_count(current_rows[0], metric.name)
            if current_rows
            else (None, 0)
        )
        prev_value, prev_count = (
            _row_value_count(previous_rows[0], metric.name)
            if previous_rows
            else (None, 0)
        )
        delta = (
            cur_value - prev_value
            if cur_value is not None and prev_value is not None
            else None
        )
        delta_pct = (
            (delta / prev_value) * 100
            if delta is not None and prev_value not in (None, 0)
            else None
        )
        connection.send_result(
            msg["id"],
            {
                "current": {"value": cur_value, "count": cur_count},
                "previous": {"value": prev_value, "count": prev_count},
                "delta": delta,
                "delta_pct": delta_pct,
            },
        )
        return

    cur_map = {
        _group_identity(row["group"]): (row["group"], row["metrics"][metric.name])
        for row in current_rows
    }
    prev_map = {
        _group_identity(row["group"]): (row["group"], row["metrics"][metric.name])
        for row in previous_rows
    }
    all_groups = list(dict.fromkeys([*cur_map.keys(), *prev_map.keys()]))
    current_list = []
    previous_list = []
    deltas = []
    for group_key in all_groups:
        group = (
            cur_map[group_key][0] if group_key in cur_map else prev_map[group_key][0]
        )
        cur = cur_map.get(group_key, (group, {"value": None, "count": 0}))[1]
        prev = prev_map.get(group_key, (group, {"value": None, "count": 0}))[1]
        current_list.append(
            {"group": group, "value": cur["value"], "count": cur["count"]}
        )
        previous_list.append(
            {"group": group, "value": prev["value"], "count": prev["count"]}
        )
        delta = (
            cur["value"] - prev["value"]
            if cur["value"] is not None and prev["value"] is not None
            else None
        )
        delta_pct = (
            (delta / prev["value"]) * 100
            if delta is not None and prev["value"] not in (None, 0)
            else None
        )
        deltas.append({"group": group, "delta": delta, "delta_pct": delta_pct})
    connection.send_result(
        msg["id"],
        {"current": current_list, "previous": previous_list, "deltas": deltas},
    )


@websocket_command(
    {
        vol.Required("type"): "custom_records/add_record",
        vol.Required(ATTR_RECORD_TYPE): str,
        vol.Required(ATTR_FIELDS): dict,
        vol.Optional(ATTR_TIMESTAMP): str,
    }
)
@async_response
async def handle_add_record(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Add a record - a thin wrapper sharing the service's validation logic."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    record_type_id = msg[ATTR_RECORD_TYPE]
    record_type = runtime_data.record_types.get(record_type_id)
    if record_type is None:
        connection.send_error(
            msg["id"], "unknown_record_type", f"Unknown record_type '{record_type_id}'"
        )
        return
    try:
        validated_fields = validate_record_data(record_type, msg[ATTR_FIELDS])
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid_fields", str(err))
        return
    try:
        timestamp = (
            _parse_datetime(msg[ATTR_TIMESTAMP]) if ATTR_TIMESTAMP in msg else None
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_datetime", str(err))
        return
    try:
        record = await runtime_data.media_store.async_add_record_with_images(
            runtime_data.storage, record_type, validated_fields, timestamp
        )
    except ImageStoreError as err:
        connection.send_error(msg["id"], "invalid_image", str(err))
        return
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_fields", str(err))
        return
    connection.send_result(msg["id"], {"record": to_public_record(record, record_type)})


@websocket_command(
    {
        vol.Required("type"): "custom_records/delete_record",
        vol.Required(ATTR_RECORD_TYPE): str,
        vol.Required("record_id"): str,
    }
)
@async_response
async def handle_delete_record(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a record by id."""
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_setup", "Custom Records is not set up")
        return
    record_type_id = msg[ATTR_RECORD_TYPE]
    if record_type_id not in runtime_data.record_types:
        connection.send_error(
            msg["id"], "unknown_record_type", f"Unknown record_type '{record_type_id}'"
        )
        return
    deleted = await runtime_data.storage.async_delete_record(
        record_type_id, msg["record_id"]
    )
    if not deleted:
        connection.send_error(msg["id"], "not_found", "Record not found")
        return
    await runtime_data.media_store.async_cleanup_orphaned_media(
        runtime_data.storage, runtime_data.record_types
    )
    connection.send_result(msg["id"], {"deleted": True})


@websocket_command(
    {
        vol.Required("type"): "custom_records/validate_image_path",
        vol.Required("path"): str,
    }
)
@async_response
async def handle_validate_image_path(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Check whether a filesystem path is a valid, existing image file."""
    error = await async_validate_image_path(hass, msg["path"])
    connection.send_result(msg["id"], {"valid": error is None, "error": error})


def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Register the custom_records WebSocket commands once, hass-wide."""
    if hass.data.get(_WS_REGISTERED_KEY):
        return
    websocket_api.async_register_command(hass, handle_list_record_types)
    websocket_api.async_register_command(hass, handle_list_records)
    websocket_api.async_register_command(hass, handle_aggregate_records)
    websocket_api.async_register_command(hass, handle_get_field_stats)
    websocket_api.async_register_command(hass, handle_histogram_records)
    websocket_api.async_register_command(hass, handle_compare_periods)
    websocket_api.async_register_command(hass, handle_add_record)
    websocket_api.async_register_command(hass, handle_delete_record)
    websocket_api.async_register_command(hass, handle_validate_image_path)
    hass.data[_WS_REGISTERED_KEY] = True
