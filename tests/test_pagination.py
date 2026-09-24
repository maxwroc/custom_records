"""Tests for opaque runtime-local pagination cursors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.custom_records.const import RecordOrder
from custom_components.custom_records.pagination import (
    CursorCache,
    CursorError,
    RecordPosition,
    query_scope,
)
from custom_components.custom_records.sql_encoding import CompiledFilter


def test_cursor_retry_and_idle_expiry() -> None:
    """Matching reads renew idle expiry and never consume the cursor."""
    cache = CursorCache()
    position = RecordPosition(-1, "opaque imported / \u00e9 ID")
    with patch("custom_components.custom_records.pagination.monotonic") as clock:
        clock.return_value = 0
        handle = cache.issue("scope", position)
        clock.return_value = 1799
        assert cache.resolve(handle, "scope") == position
        clock.return_value = 3598
        assert cache.resolve(handle, "scope") == position
        clock.return_value = 5398
        with pytest.raises(CursorError) as error:
            cache.resolve(handle, "scope")
    assert error.value.code == "cursor_expired"


@pytest.mark.parametrize("handle", [None, 1, {}, "", "x" * 42, "x" * 44, "!" * 43])
def test_invalid_cursor(handle: object) -> None:
    """Reject malformed handles before touching cache state."""
    with pytest.raises(CursorError) as error:
        CursorCache().resolve(handle, "scope")
    assert error.value.code == "invalid_cursor"


def test_cursor_scope_and_runtime_isolation() -> None:
    """Wrong queries do not renew idle lifetime; another runtime cannot resolve."""
    cache = CursorCache()
    with patch("custom_components.custom_records.pagination.monotonic") as clock:
        clock.return_value = 0
        handle = cache.issue("scope", RecordPosition(123, "id"))
        clock.return_value = 1799
        with pytest.raises(CursorError) as mismatch:
            cache.resolve(handle, "other")
        assert mismatch.value.code == "cursor_query_mismatch"
        with pytest.raises(CursorError) as restarted:
            CursorCache().resolve(handle, "scope")
        assert restarted.value.code == "cursor_expired"
        clock.return_value = 1800
        with pytest.raises(CursorError) as expired:
            cache.resolve(handle, "scope")
        assert expired.value.code == "cursor_expired"


def test_cursor_entry_budget_uses_lru() -> None:
    """Only the least recently used handle is evicted when capacity is reached."""
    cache = CursorCache(max_entries=2)
    position = RecordPosition(123, "id")
    first = cache.issue("scope", position)
    second = cache.issue("scope", position)
    cache.resolve(first, "scope")
    third = cache.issue("scope", position)
    assert cache.resolve(first, "scope") == position
    assert cache.resolve(third, "scope") == position
    with pytest.raises(CursorError) as error:
        cache.resolve(second, "scope")
    assert error.value.code == "cursor_expired"


def test_cursor_byte_budget_and_oversized_id() -> None:
    """Large opaque IDs count toward memory and are never truncated."""
    cache = CursorCache(max_bytes=6000)
    position = RecordPosition(123, "x" * 3500)
    first = cache.issue("scope", position)
    second = cache.issue("scope", position)
    with pytest.raises(CursorError) as evicted:
        cache.resolve(first, "scope")
    assert evicted.value.code == "cursor_expired"
    with pytest.raises(CursorError) as oversized:
        cache.issue("scope", RecordPosition(123, "x" * 6000))
    assert oversized.value.code == "pagination_resource_limit"
    assert cache.resolve(second, "scope") == position


def test_expired_entries_release_budget() -> None:
    """Expiring a handle releases its accounting before inserting another."""
    cache = CursorCache(max_bytes=6000)
    position = RecordPosition(123, "x" * 3500)
    with patch("custom_components.custom_records.pagination.monotonic") as clock:
        clock.return_value = 0
        first = cache.issue("scope", position)
        clock.return_value = 1800
        second = cache.issue("scope", position)
        with pytest.raises(CursorError) as error:
            cache.resolve(first, "scope")
        assert error.value.code == "cursor_expired"
        assert cache.resolve(second, "scope") == position


def test_query_scope_normalizes_instants_and_binds_query() -> None:
    """Offset spellings normalize, but exact microseconds/filter/type do not."""
    start = datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    end = start + timedelta(days=1)
    where = CompiledFilter('"amount" > ?', [1.0])
    scope = query_scope("entry", "type", start, end, where, RecordOrder.DESC)
    assert query_scope("entry", "type", start, end, where, RecordOrder.ASC) != scope
    same_start = datetime.fromisoformat("1970-01-01T00:59:59.999999+01:00")
    assert (
        query_scope("entry", "type", same_start, end, where, RecordOrder.DESC) == scope
    )
    assert query_scope("other", "type", start, end, where, RecordOrder.DESC) != scope
    assert query_scope("entry", "other", start, end, where, RecordOrder.DESC) != scope
    assert query_scope("entry", "type", start, None, where, RecordOrder.DESC) != scope
    assert query_scope("entry", "type", None, end, where, RecordOrder.DESC) != scope
    assert (
        query_scope(
            "entry",
            "type",
            start + timedelta(microseconds=1),
            end,
            where,
            RecordOrder.DESC,
        )
        != scope
    )
    assert (
        query_scope(
            "entry",
            "type",
            start,
            end,
            CompiledFilter('"amount" > ?', [2.0]),
            RecordOrder.DESC,
        )
        != scope
    )


@pytest.mark.parametrize(
    "limits", [{"max_entries": 0}, {"max_bytes": 0}, {"idle_seconds": 0}]
)
def test_cache_rejects_nonpositive_limits(limits: dict[str, int]) -> None:
    """Configuration cannot accidentally create an unbounded/invalid cache."""
    with pytest.raises(ValueError, match="limits must be positive"):
        CursorCache(**limits)
