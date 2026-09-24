"""Exact, bounded keyset pagination over live SQLite records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_records.const import FieldType, RecordOrder
from custom_components.custom_records.csv_transfer import ImportRow
from custom_components.custom_records.models import FieldDefinition, RecordType
from custom_components.custom_records.pagination import RecordPosition
from custom_components.custom_records.sql_encoding import (
    CompiledFilter,
    to_epoch_micros,
)
from custom_components.custom_records.store import RecordStorage

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def storage(hass: HomeAssistant) -> AsyncGenerator[RecordStorage]:
    """Close the worker and database after each pagination scenario."""
    store = RecordStorage(hass, "pagination")
    await store.async_load(
        {
            "visits": RecordType(
                id="visits",
                name="Visits",
                fields=[FieldDefinition(key="n", label="N", type=FieldType.NUMBER)],
            )
        }
    )
    yield store
    await store.async_close()


@pytest.mark.parametrize("count", [0, 1, 19, 20, 21, 500, 501, 1001])
@pytest.mark.parametrize("order", list(RecordOrder))
async def test_complete_same_timestamp_traversal(
    storage: RecordStorage, count: int, order: RecordOrder
) -> None:
    """Every tied record appears exactly once, including well beyond 500."""
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    ids = [f"imported:{n:04d}" for n in range(count)]
    await storage.async_import_records(
        "visits",
        [
            ImportRow(id=record_id, timestamp=timestamp, fields={"n": n})
            for n, record_id in enumerate(ids)
        ],
    )
    before = None
    seen = []
    while True:
        page = await storage.async_list_records(
            "visits", limit=20, position=before, order=order
        )
        assert len(page.records) <= 20
        seen.extend(row["id"] for row in page.records)
        if page.next_position is None:
            break
        assert page.next_position.record_id == page.records[-1]["id"]
        assert page.next_position.timestamp_micros == to_epoch_micros(timestamp)
        assert page.next_position != before
        before = page.next_position
    assert seen == sorted(ids, reverse=order is RecordOrder.DESC)


@pytest.mark.parametrize("order", list(RecordOrder))
async def test_exact_ranges_filters_and_binary_ids(
    storage: RecordStorage, order: RecordOrder
) -> None:
    """Pre-epoch microseconds, inclusive endpoints, and opaque IDs survive paging."""
    timestamp = datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    ids = ["Z", "a", "\u00e9 / ' ?", "\u0000embedded", "\U0001f697", "x" * 5000]
    await storage.async_import_records(
        "visits",
        [
            ImportRow(id=record_id, timestamp=timestamp, fields={"n": n})
            for n, record_id in enumerate(ids)
        ]
        + [
            ImportRow(
                id="older",
                timestamp=timestamp - timedelta(microseconds=1),
                fields={"n": 100},
            ),
            ImportRow(
                id="newer",
                timestamp=timestamp + timedelta(microseconds=1),
                fields={"n": 101},
            ),
        ],
    )
    expected = sorted(
        ids[1:],
        key=lambda value: value.encode("utf-8"),
        reverse=order is RecordOrder.DESC,
    )
    before = None
    seen = []
    while True:
        page = await storage.async_list_records(
            "visits",
            limit=1,
            start=timestamp,
            end=timestamp,
            where=CompiledFilter('"n" >= ?', [1]),
            position=before,
            order=order,
        )
        seen.extend(row["id"] for row in page.records)
        if page.next_position is None:
            break
        assert page.next_position.timestamp_micros == -1
        before = page.next_position
    assert seen == expected
    page = await storage.async_list_records("visits", limit=1, order=RecordOrder.DESC)
    assert page.records[0]["id"] == "newer"
    assert page.next_position is not None
    assert page.next_position.timestamp_micros == 0
    next_page = await storage.async_list_records(
        "visits", limit=500, position=page.next_position, order=RecordOrder.DESC
    )
    assert len(next_page.records) == len(ids) + 1
    assert next_page.records[-1]["id"] == "older"


async def test_live_inserts_deleted_boundary_and_retry(storage: RecordStorage) -> None:
    """No boundary lookup or offset is needed when rows change between requests."""
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    await storage.async_import_records(
        "visits",
        [
            ImportRow(id=str(n), timestamp=timestamp, fields={"n": n})
            for n in (2, 4, 6, 8)
        ],
    )
    first = await storage.async_list_records("visits", limit=2, order=RecordOrder.DESC)
    assert [row["id"] for row in first.records] == ["8", "6"]
    assert first.next_position is not None
    await storage.async_delete_record("visits", "6")
    await storage.async_import_records(
        "visits",
        [
            ImportRow(id=str(n), timestamp=timestamp, fields={"n": n})
            for n in (3, 5, 7, 9)
        ],
    )
    page = await storage.async_list_records(
        "visits", limit=3, position=first.next_position, order=RecordOrder.DESC
    )
    assert [row["id"] for row in page.records] == ["5", "4", "3"]
    retry = await storage.async_list_records(
        "visits", limit=3, position=first.next_position, order=RecordOrder.DESC
    )
    assert retry == page
    await storage.async_enforce_max_records({"visits": 2})
    exhausted = await storage.async_list_records(
        "visits", limit=3, position=first.next_position, order=RecordOrder.DESC
    )
    assert exhausted.records == []
    assert exhausted.next_position is None


@pytest.mark.parametrize("order", list(RecordOrder))
async def test_internal_limits_and_unlimited_order(
    storage: RecordStorage, order: RecordOrder
) -> None:
    """Internal limits are explicit, uncapped, and never change ordering."""
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    await storage.async_import_records(
        "visits",
        [
            ImportRow(
                id=str(n),
                timestamp=timestamp + timedelta(microseconds=n),
                fields={"n": n},
            )
            for n in range(501)
        ],
    )
    page = await storage.async_list_records("visits", limit=5000, order=order)
    assert len(page.records) == 501
    assert page.next_position is None
    unlimited = await storage.async_list_records("visits", order=order)
    assert unlimited == page
    expected = list(range(501))
    if order is RecordOrder.DESC:
        expected.reverse()
    assert [row["id"] for row in unlimited.records] == [str(n) for n in expected]
    limited = await storage.async_list_records("visits", limit=20, order=order)
    assert limited.records == unlimited.records[:20]
    assert limited.next_position is not None
    suffix = await storage.async_list_records(
        "visits", position=limited.next_position, order=order
    )
    assert suffix.records == unlimited.records[20:]
    assert suffix.next_position is None
    with pytest.raises(ValueError, match="Page size"):
        await storage.async_list_records("visits", limit=0, order=RecordOrder.DESC)


@pytest.mark.parametrize("order", list(RecordOrder))
async def test_pagination_query_seeks_index_with_bounded_lookahead(
    storage: RecordStorage,
    order: RecordOrder,
) -> None:
    """The generated query seeks the composite index with at most 501 rows."""
    statements: list[str] = []
    connection = storage._require_conn()  # noqa: SLF001
    await storage._run(connection.set_trace_callback, statements.append)  # noqa: SLF001
    await storage.async_list_records(
        "visits",
        limit=500,
        position=RecordPosition(1000, "opaque id"),
        order=order,
    )
    query = statements[-1]
    assert query.endswith("LIMIT 501")
    assert "OFFSET" not in query

    def explain() -> list[str]:
        return [
            row[3]
            for row in connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        ]

    plan = await storage._run(explain)  # noqa: SLF001
    assert any(
        "SEARCH" in detail and "idx_records_visits_ts" in detail for detail in plan
    )
    assert not any("TEMP B-TREE" in detail for detail in plan)


async def test_ascending_live_inserts_and_deleted_boundary(
    storage: RecordStorage,
) -> None:
    """Ascending continuation admits only new keys on the untraversed side."""
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    await storage.async_import_records(
        "visits",
        [
            ImportRow(id=str(n), timestamp=timestamp, fields={"n": n})
            for n in (2, 4, 6, 8)
        ],
    )
    first = await storage.async_list_records("visits", order=RecordOrder.ASC, limit=2)
    assert [r["id"] for r in first.records] == ["2", "4"]
    assert first.next_position is not None
    await storage.async_delete_record("visits", "4")
    await storage.async_import_records(
        "visits",
        [
            ImportRow(id=str(n), timestamp=timestamp, fields={"n": n})
            for n in (1, 3, 5, 7, 9)
        ],
    )
    page = await storage.async_list_records(
        "visits", order=RecordOrder.ASC, limit=3, position=first.next_position
    )
    assert [r["id"] for r in page.records] == ["5", "6", "7"]
    await storage.async_purge_expired({"visits": 1})
    empty = await storage.async_list_records(
        "visits", order=RecordOrder.ASC, limit=3, position=first.next_position
    )
    assert empty.records == []
    assert empty.next_position is None


async def test_missing_type_returns_consistent_empty_result(
    storage: RecordStorage,
) -> None:
    """Unknown internal record types retain their prior empty-result behavior."""
    for limit in (None, 1):
        result = await storage.async_list_records(
            "missing", order=RecordOrder.ASC, limit=limit
        )
        assert result.records == []
        assert result.next_position is None
