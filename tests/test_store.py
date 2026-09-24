"""Tests for custom_records.store.RecordStorage (SQLite-backed, plan_sql.md)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.custom_records.const import (
    ATTR_ENTRY_ID,
    ATTR_RECORD_TYPE,
    EVENT_RECORDS_UPDATED,
    FieldType,
    RecordOrder,
)
from custom_components.custom_records.csv_transfer import ImportRow
from custom_components.custom_records.models import FieldDefinition, RecordType
from custom_components.custom_records.sql_encoding import CompiledFilter
from custom_components.custom_records.store import (
    RecordStorage,
    SchemaError,
    _transaction,
)


def _bp_record_type() -> RecordType:
    """Build a small record type with two optional NUMBER fields for fixtures."""
    return RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            FieldDefinition(key="systolic", label="Systolic", type=FieldType.NUMBER),
            FieldDefinition(key="i", label="i", type=FieldType.NUMBER),
        ],
    )


def _weight_record_type() -> RecordType:
    return RecordType(
        id="weight",
        name="Weight",
        fields=[FieldDefinition(key="kg", label="kg", type=FieldType.NUMBER)],
    )


def _capture_updated_events(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return a list that accumulates EVENT_RECORDS_UPDATED payloads as they fire."""
    captured: list[dict[str, Any]] = []
    hass.bus.async_listen(
        EVENT_RECORDS_UPDATED, lambda event: captured.append(dict(event.data))
    )
    return captured


async def test_add_list_delete_record(hass: HomeAssistant) -> None:
    """Records can be added, listed, and deleted by id."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})

    record = await storage.async_add_record("bp", {"systolic": 120})
    assert await storage.async_record_count("bp") == 1
    listed = (await storage.async_list_records("bp", order=RecordOrder.ASC)).records
    assert listed == [record]

    assert await storage.async_delete_record("bp", record["id"]) is True
    assert await storage.async_record_count("bp") == 0
    assert await storage.async_delete_record("bp", "unknown-id") is False


async def test_get_record_by_id(hass: HomeAssistant) -> None:
    """A primary-key lookup returns the envelope, or None when absent."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    record = await storage.async_add_record("bp", {"systolic": 120})
    await storage.async_add_record("bp", {"systolic": 130})

    assert await storage.async_get_record("bp", record["id"]) == record
    assert await storage.async_get_record("bp", "missing") is None
    assert await storage.async_get_record("unknown", record["id"]) is None


async def test_list_image_references_reads_only_image_columns(
    hass: HomeAssistant,
) -> None:
    """Only records with images are returned, oldest first, per image field."""
    record_type = RecordType(
        id="pets",
        name="Pets",
        fields=[
            FieldDefinition(key="name", label="Name", type=FieldType.TEXT),
            FieldDefinition(key="front", label="Front", type=FieldType.IMAGE),
            FieldDefinition(key="back", label="Back", type=FieldType.IMAGE),
        ],
    )
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"pets": record_type, "bp": _bp_record_type()})
    now = dt_util.utcnow()
    newer = await storage.async_add_record(
        "pets", {"name": "b", "back": "b.png"}, timestamp=now
    )
    await storage.async_add_record("pets", {"name": "no image"}, timestamp=now)
    older = await storage.async_add_record(
        "pets",
        {"name": "a", "front": "a1.jpg", "back": "a2.jpg"},
        timestamp=now - timedelta(days=1),
    )

    statements: list[str] = []
    conn = storage._require_conn()  # noqa: SLF001
    await storage._run(conn.set_trace_callback, statements.append)  # noqa: SLF001
    references = await storage.async_list_image_references("pets")
    await storage._run(conn.set_trace_callback, None)  # noqa: SLF001

    assert references == [
        (older["id"], {"front": "a1.jpg", "back": "a2.jpg"}),
        (newer["id"], {"back": "b.png"}),
    ]
    assert len(statements) == 1
    assert "SELECT *" not in statements[0]
    assert '"name"' not in statements[0]
    assert await storage.async_list_image_references("bp") == []
    assert await storage.async_list_image_references("unknown") == []


async def test_list_records_limit_sorts_desc_and_truncates(hass: HomeAssistant) -> None:
    """Limit sorts newest-first (by timestamp) and truncates to at most `limit`."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    now = dt_util.utcnow()
    for i in range(5):
        await storage.async_add_record(
            "bp", {"i": i}, timestamp=now + timedelta(seconds=i)
        )

    limited = (
        await storage.async_list_records("bp", limit=2, order=RecordOrder.DESC)
    ).records
    assert [r["d"]["i"] for r in limited] == [4, 3]

    unlimited = (await storage.async_list_records("bp", order=RecordOrder.ASC)).records
    assert len(unlimited) == 5
    # The caller explicitly chooses chronological order for this full read.
    assert [r["d"]["i"] for r in unlimited] == [0, 1, 2, 3, 4]


async def test_list_records_where_filters_and_combines_with_limit(
    hass: HomeAssistant,
) -> None:
    """A compiled SQL `where` keeps only matching rows, applied in the same query."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    now = dt_util.utcnow()
    for i in range(5):
        await storage.async_add_record(
            "bp", {"i": i}, timestamp=now + timedelta(seconds=i)
        )

    where = CompiledFilter(sql='"i" % 2 = 0')
    filtered = (
        await storage.async_list_records("bp", where=where, order=RecordOrder.ASC)
    ).records
    assert [r["d"]["i"] for r in filtered] == [0, 2, 4]

    filtered_limited = (
        await storage.async_list_records(
            "bp", where=where, limit=1, order=RecordOrder.DESC
        )
    ).records
    assert [r["d"]["i"] for r in filtered_limited] == [4]


async def test_records_persist_across_instances(hass: HomeAssistant) -> None:
    """Records written by one RecordStorage are loadable by a fresh instance."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    await storage.async_add_record("bp", {"systolic": 120})
    await storage.async_close()

    reloaded = RecordStorage(hass, "entry1")
    await reloaded.async_load({"bp": _bp_record_type()})
    assert await reloaded.async_record_count("bp") == 1
    await reloaded.async_close()


async def test_retention_none_keeps_forever(hass: HomeAssistant) -> None:
    """retention_days=None means old records are never purged."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    await storage.async_add_record(
        "bp", {"systolic": 120}, timestamp=dt_util.utcnow() - timedelta(days=1000)
    )

    removed = await storage.async_purge_expired({"bp": None})
    assert removed == {"bp": 0}
    assert await storage.async_record_count("bp") == 1


async def test_purge_expired_removes_old_records(hass: HomeAssistant) -> None:
    """Records older than retention_days are removed; newer ones are kept."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    old_ts = dt_util.utcnow() - timedelta(days=10)
    new_ts = dt_util.utcnow()
    await storage.async_add_record("bp", {"systolic": 1}, timestamp=old_ts)
    await storage.async_add_record("bp", {"systolic": 2}, timestamp=new_ts)

    removed = await storage.async_purge_expired({"bp": 5})
    assert removed == {"bp": 1}
    remaining = (await storage.async_list_records("bp", order=RecordOrder.ASC)).records
    assert len(remaining) == 1
    assert remaining[0]["d"]["systolic"] == 2


async def test_invalid_retention_is_safe_noop(hass: HomeAssistant) -> None:
    """Malformed persisted retention values never purge records."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    await storage.async_add_record("bp", {"systolic": 1})

    assert await storage.async_purge_expired({"bp": 0}) == {"bp": 0}
    assert await storage.async_record_count("bp") == 1


async def test_max_records_none_is_unlimited(hass: HomeAssistant) -> None:
    """max_records=None means no count-based cap is enforced."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    for i in range(3):
        await storage.async_add_record("bp", {"i": i})

    removed = await storage.async_enforce_max_records({"bp": None})
    assert removed == {"bp": 0}
    assert await storage.async_record_count("bp") == 3


async def test_max_records_enforced_drops_oldest(hass: HomeAssistant) -> None:
    """When over the cap, the oldest records are dropped first."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    now = dt_util.utcnow()
    for i in range(3):
        await storage.async_add_record(
            "bp", {"i": i}, timestamp=now + timedelta(seconds=i)
        )

    removed = await storage.async_enforce_max_records({"bp": 2})
    assert removed == {"bp": 1}
    remaining = (await storage.async_list_records("bp", order=RecordOrder.ASC)).records
    assert [r["d"]["i"] for r in remaining] == [1, 2]


async def test_invalid_max_records_is_safe_noop(hass: HomeAssistant) -> None:
    """Malformed persisted max_records values never mutate records."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    await storage.async_add_record("bp", {"systolic": 1})

    assert await storage.async_enforce_max_records({"bp": 0}) == {"bp": 0}
    assert await storage.async_record_count("bp") == 1


async def test_async_remove_deletes_database_file(hass: HomeAssistant) -> None:
    """async_remove() deletes the on-disk database file for the entry."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    await storage.async_add_record("bp", {"i": 1})

    await storage.async_remove()

    reloaded = RecordStorage(hass, "entry1")
    await reloaded.async_load({"bp": _bp_record_type()})
    assert await reloaded.async_record_count("bp") == 0
    await reloaded.async_close()


async def test_add_record_fires_updated_event(hass: HomeAssistant) -> None:
    """Adding a record fires EVENT_RECORDS_UPDATED with the entry/type ids."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    captured = _capture_updated_events(hass)

    await storage.async_add_record("bp", {"systolic": 120})
    await hass.async_block_till_done()

    assert captured == [{ATTR_ENTRY_ID: "entry1", ATTR_RECORD_TYPE: "bp"}]


async def test_delete_record_fires_updated_event_only_when_removed(
    hass: HomeAssistant,
) -> None:
    """Deleting fires the event only when a record was actually removed."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    record = await storage.async_add_record("bp", {"systolic": 120})
    captured = _capture_updated_events(hass)

    assert await storage.async_delete_record("bp", "unknown-id") is False
    await hass.async_block_till_done()
    assert captured == []

    assert await storage.async_delete_record("bp", record["id"]) is True
    await hass.async_block_till_done()
    assert captured == [{ATTR_ENTRY_ID: "entry1", ATTR_RECORD_TYPE: "bp"}]


async def test_purge_expired_fires_updated_event_only_when_removed(
    hass: HomeAssistant,
) -> None:
    """Purging fires the event only for types that actually lost records."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type(), "weight": _weight_record_type()})
    await storage.async_add_record(
        "bp", {"systolic": 1}, timestamp=dt_util.utcnow() - timedelta(days=10)
    )
    await storage.async_add_record("weight", {"kg": 70})
    captured = _capture_updated_events(hass)

    removed = await storage.async_purge_expired({"bp": 5, "weight": None})
    await hass.async_block_till_done()

    assert removed == {"bp": 1, "weight": 0}
    assert captured == [{ATTR_ENTRY_ID: "entry1", ATTR_RECORD_TYPE: "bp"}]


async def test_max_records_enforced_fires_updated_event_only_when_removed(
    hass: HomeAssistant,
) -> None:
    """max_records eviction fires the event only for types that lost records."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type(), "weight": _weight_record_type()})
    now = dt_util.utcnow()
    for i in range(3):
        await storage.async_add_record(
            "bp", {"i": i}, timestamp=now + timedelta(seconds=i)
        )
    await storage.async_add_record("weight", {"kg": 70})
    captured = _capture_updated_events(hass)

    removed = await storage.async_enforce_max_records({"bp": 2, "weight": None})
    await hass.async_block_till_done()

    assert removed == {"bp": 1, "weight": 0}
    assert captured == [{ATTR_ENTRY_ID: "entry1", ATTR_RECORD_TYPE: "bp"}]


async def test_import_records_appends_new_rows(hass: HomeAssistant) -> None:
    """Rows with a fresh id are appended, generating uuid/timestamp when unset."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})

    summary = await storage.async_import_records(
        "bp",
        [
            ImportRow(id="row-1", timestamp=dt_util.utcnow(), fields={"i": 1}),
            ImportRow(id=None, timestamp=None, fields={"i": 2}),
        ],
    )

    assert summary.imported == 2
    assert summary.skipped_duplicate == 0
    assert await storage.async_record_count("bp") == 2
    records = (await storage.async_list_records("bp", order=RecordOrder.ASC)).records
    ids = {r["id"] for r in records}
    assert "row-1" in ids


async def test_import_records_skips_duplicate_ids(hass: HomeAssistant) -> None:
    """A row whose id already exists in the store is skipped, not overwritten."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    existing = await storage.async_add_record("bp", {"i": 0})

    summary = await storage.async_import_records(
        "bp",
        [
            ImportRow(id=existing["id"], timestamp=dt_util.utcnow(), fields={"i": 99}),
            ImportRow(id="new-row", timestamp=dt_util.utcnow(), fields={"i": 1}),
        ],
    )

    assert summary.imported == 1
    assert summary.skipped_duplicate == 1
    assert await storage.async_record_count("bp") == 2
    # The original record's data is untouched (not overwritten).
    records = (await storage.async_list_records("bp", order=RecordOrder.ASC)).records
    original = next(r for r in records if r["id"] == existing["id"])
    assert original["d"]["i"] == 0


async def test_import_records_fires_updated_event_once_not_per_row(
    hass: HomeAssistant,
) -> None:
    """Importing multiple rows fires EVENT_RECORDS_UPDATED once for the call."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    captured = _capture_updated_events(hass)

    await storage.async_import_records(
        "bp",
        [
            ImportRow(id=None, timestamp=None, fields={"i": 1}),
            ImportRow(id=None, timestamp=None, fields={"i": 2}),
            ImportRow(id=None, timestamp=None, fields={"i": 3}),
        ],
    )
    await hass.async_block_till_done()

    assert captured == [{ATTR_ENTRY_ID: "entry1", ATTR_RECORD_TYPE: "bp"}]


async def test_import_records_no_rows_does_not_fire_event(
    hass: HomeAssistant,
) -> None:
    """Importing zero new rows (e.g. all duplicates) does not fire the event."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    existing = await storage.async_add_record("bp", {"i": 0})
    captured = _capture_updated_events(hass)

    summary = await storage.async_import_records(
        "bp", [ImportRow(id=existing["id"], timestamp=None, fields={"i": 99})]
    )
    await hass.async_block_till_done()

    assert summary.imported == 0
    assert captured == []


async def test_import_records_skips_content_duplicate_without_id(
    hass: HomeAssistant,
) -> None:
    """A no-id row with the SAME timestamp+fields as an existing record is skipped."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    ts = dt_util.utcnow()
    await storage.async_add_record("bp", {"systolic": 120}, timestamp=ts)

    summary = await storage.async_import_records(
        "bp", [ImportRow(id=None, timestamp=ts, fields={"systolic": 120})]
    )

    assert summary.imported == 0
    assert summary.skipped_duplicate == 1
    assert await storage.async_record_count("bp") == 1


async def test_import_records_same_timestamp_different_fields_is_imported(
    hass: HomeAssistant,
) -> None:
    """Same timestamp but different field data is NOT treated as a duplicate."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    ts = dt_util.utcnow()
    await storage.async_add_record("bp", {"systolic": 120}, timestamp=ts)

    summary = await storage.async_import_records(
        "bp", [ImportRow(id=None, timestamp=ts, fields={"systolic": 130})]
    )

    assert summary.imported == 1
    assert summary.skipped_duplicate == 0
    assert await storage.async_record_count("bp") == 2


async def test_import_records_same_fields_different_timestamp_is_imported(
    hass: HomeAssistant,
) -> None:
    """Identical field data at a different timestamp is NOT treated as a duplicate."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    ts = dt_util.utcnow()
    await storage.async_add_record("bp", {"systolic": 120}, timestamp=ts)

    summary = await storage.async_import_records(
        "bp",
        [
            ImportRow(
                id=None, timestamp=ts + timedelta(seconds=1), fields={"systolic": 120}
            )
        ],
    )

    assert summary.imported == 1
    assert summary.skipped_duplicate == 0
    assert await storage.async_record_count("bp") == 2


async def test_import_records_dedupes_content_duplicates_within_same_batch(
    hass: HomeAssistant,
) -> None:
    """Two no-id rows in the same import with identical timestamp+fields dedupe."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    ts = dt_util.utcnow()

    summary = await storage.async_import_records(
        "bp",
        [
            ImportRow(id=None, timestamp=ts, fields={"systolic": 120}),
            ImportRow(id=None, timestamp=ts, fields={"systolic": 120}),
        ],
    )

    assert summary.imported == 1
    assert summary.skipped_duplicate == 1
    assert await storage.async_record_count("bp") == 1


async def test_import_records_without_timestamp_never_content_deduped(
    hass: HomeAssistant,
) -> None:
    """Rows with neither id nor timestamp are always appended (nothing to compare)."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})

    summary = await storage.async_import_records(
        "bp",
        [
            ImportRow(id=None, timestamp=None, fields={"systolic": 120}),
            ImportRow(id=None, timestamp=None, fields={"systolic": 120}),
        ],
    )

    assert summary.imported == 2
    assert summary.skipped_duplicate == 0
    assert await storage.async_record_count("bp") == 2


async def test_add_field_alters_existing_table(hass: HomeAssistant) -> None:
    """Preparing a newly-added optional field adds its column, not a rebuild."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    await storage.async_add_record("bp", {"systolic": 120})

    extended = RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            *_bp_record_type().fields,
            FieldDefinition(key="pulse", label="Pulse", type=FieldType.NUMBER),
        ],
    )
    await storage.async_ensure_record_type(extended)
    record = await storage.async_add_record("bp", {"systolic": 130, "pulse": 65})

    records = (await storage.async_list_records("bp", order=RecordOrder.ASC)).records
    assert len(records) == 2
    assert record["d"]["pulse"] == 65


async def test_required_field_missing_from_existing_table_raises(
    hass: HomeAssistant,
) -> None:
    """Adding a REQUIRED field to an already-created table is a schema error."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})

    with_required = RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            *_bp_record_type().fields,
            FieldDefinition(
                key="pulse", label="Pulse", type=FieldType.NUMBER, required=True
            ),
        ],
    )
    try:
        await storage.async_ensure_record_type(with_required)
    except SchemaError:
        pass
    else:
        msg = "Expected SchemaError for a required field missing from an existing table"
        raise AssertionError(msg)


async def test_missing_configured_table_is_not_recreated(
    hass: HomeAssistant,
) -> None:
    """An initialized database never silently recreates a missing configured table."""
    storage = RecordStorage(hass, "entry1")
    await storage.async_load({"bp": _bp_record_type()})
    await storage.async_close()

    db_path = hass.config.path(".storage", "custom_records", "custom_records_entry1.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute('DROP TABLE "records_bp"')

    reloaded = RecordStorage(hass, "entry1")
    try:
        with pytest.raises(SchemaError, match="missing its database table"):
            await reloaded.async_load({"bp": _bp_record_type()})
    finally:
        await reloaded.async_close()


def test_commit_failure_rolls_back_connection() -> None:
    """A deferred constraint failure at COMMIT leaves the connection reusable."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    )

    with pytest.raises(sqlite3.IntegrityError), _transaction(conn):
        conn.execute("INSERT INTO child VALUES (1)")

    assert conn.in_transaction is False
    with _transaction(conn):
        conn.execute("INSERT INTO parent VALUES (1)")
    conn.close()
