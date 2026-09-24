"""Public pagination ordering, compatibility, and cursor error contracts."""

# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.custom_records.csv_transfer import ImportRow
from custom_components.custom_records.pagination import CursorCache

from .conftest import BP_RECORD_TYPE, async_setup_entry_with_types

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.typing import WebSocketGenerator


async def test_capabilities_removed(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The prerelease API has no capability/version negotiation endpoint."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "custom_records/get_capabilities"})
    response = await client.receive_json()
    assert response["error"]["code"] == "unknown_command"


async def test_listing_not_setup(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Not loaded remains an explicit listing error."""
    assert await async_setup_component(hass, "custom_records", {})
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "custom_records/list_records", "record_type": "bp"}
    )
    response = await client.receive_json()
    assert response["error"]["code"] == "not_setup"


@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_paginate_1001_tied_records(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, order: str
) -> None:
    """Traverse beyond 500 over WS and keep each response bounded."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    timestamp = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=f"opaque {n:04d}", timestamp=timestamp, fields={"systolic": n})
            for n in range(1001)
        ],
    )
    client = await hass_ws_client(hass)
    query: dict[str, Any] = {
        "type": "custom_records/list_records",
        "record_type": "bp",
        "paginate": True,
        "order": order,
    }
    seen = []
    result: dict[str, Any] = {}
    for request_id in range(1, 4):
        await client.send_json({"id": request_id, **query})
        response = await client.receive_json()
        assert response["success"]
        result = response["result"]
        assert set(result) == {"records", "has_more", "next_cursor"}
        assert len(result["records"]) <= 500
        assert result["has_more"] == (result["next_cursor"] is not None)
        seen.extend(row["id"] for row in result["records"])
        if not result["has_more"]:
            break
        query["cursor"] = result["next_cursor"]
        query["limit"] = 5000
    assert result["has_more"] is False
    assert seen == sorted(
        [f"opaque {n:04d}" for n in range(1001)], reverse=order == "desc"
    )
    await client.send_json(
        {
            "id": 4,
            "type": "custom_records/list_records",
            "record_type": "bp",
        }
    )
    legacy = (await client.receive_json())["result"]
    assert set(legacy) == {"records"}
    assert len(legacy["records"]) == 500


@pytest.mark.parametrize("count", [0, 1, 19, 20, 21])
async def test_page_lookahead_contract(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, count: int
) -> None:
    """A full final page is exhausted, while a single extra row yields a cursor."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=str(n), timestamp=None, fields={"systolic": n})
            for n in range(count)
        ],
    )
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "paginate": True,
            "limit": 20,
        }
    )
    result = (await client.receive_json())["result"]
    assert len(result["records"]) == min(count, 20)
    assert result["has_more"] == (count > 20)
    assert (result["next_cursor"] is not None) == (count > 20)


@pytest.mark.parametrize("cursor", ["", 5, {}, "x" * 10000, "!" * 43])
async def test_invalid_cursor_shapes(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, cursor: object
) -> None:
    """Bad tokens get the actionable cursor error rather than an empty page."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "paginate": True,
            "cursor": cursor,
        }
    )
    response = await client.receive_json()
    assert response["error"]["code"] == "invalid_cursor"


async def test_null_cursor_reads_first_page_and_retries_reuse_handle(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`cursor: null` starts from the top; repeating a page returns one handle."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=str(n), timestamp=None, fields={"systolic": n})
            for n in range(3)
        ],
    )
    client = await hass_ws_client(hass)
    results = []
    for msg_id, cursor in enumerate((None, None), start=1):
        await client.send_json(
            {
                "id": msg_id,
                "type": "custom_records/list_records",
                "record_type": "bp",
                "paginate": True,
                "limit": 2,
                "cursor": cursor,
            }
        )
        results.append((await client.receive_json())["result"])
    assert [r["id"] for r in results[0]["records"]] == ["2", "1"]
    assert results[0]["has_more"] is True
    assert results[1]["next_cursor"] == results[0]["next_cursor"]


@pytest.mark.parametrize("paginate", [False, None])
@pytest.mark.parametrize("cursor", ["a" * 43, None])
async def test_cursor_requires_opt_in(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    *,
    paginate: bool | None,
    cursor: str | None,
) -> None:
    """Neither absent nor false pagination silently ignores a cursor."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)
    request: dict[str, Any] = {
        "id": 1,
        "type": "custom_records/list_records",
        "record_type": "bp",
        "cursor": cursor,
    }
    if paginate is not None:
        request["paginate"] = paginate
    await client.send_json(request)
    response = await client.receive_json()
    assert response["error"]["code"] == "invalid_cursor"


async def test_scope_normalization_retry_size_change_and_expiry(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Normalized scope stays bound across requests, retries, and cache replacement."""
    entry = await async_setup_entry_with_types(
        hass, [BP_RECORD_TYPE, {**BP_RECORD_TYPE, "id": "other"}]
    )
    timestamp = datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=f"id {n}", timestamp=timestamp, fields={"systolic": n})
            for n in range(5)
        ],
    )
    client = await hass_ws_client(hass)
    query: dict[str, Any] = {
        "type": "custom_records/list_records",
        "record_type": "bp",
        "paginate": True,
        "limit": 1,
        "start": timestamp.isoformat(),
        "end": timestamp.isoformat(),
        "filter": [{"systolic": ">= 0"}],
    }
    await client.send_json({"id": 1, **query})
    first = (await client.receive_json())["result"]
    query["cursor"] = first["next_cursor"]
    mismatches = [
        {"order": "asc"},
        {"record_type": "other"},
        {"filter": [{"systolic": ">= 1"}]},
        {"start": (timestamp - timedelta(microseconds=1)).isoformat()},
        {"end": (timestamp + timedelta(microseconds=1)).isoformat()},
    ]
    for request_id, change in enumerate(mismatches, 2):
        await client.send_json({"id": request_id, **query, **change})
        response = await client.receive_json()
        assert response["error"]["code"] == "cursor_query_mismatch"
    query.update(limit=3, start="1970-01-01T00:59:59.999999+01:00")
    for request_id in (7, 8):
        await client.send_json({"id": request_id, **query})
        result = (await client.receive_json())["result"]
        assert [row["id"] for row in result["records"]] == ["id 3", "id 2", "id 1"]
        assert result["has_more"]
    entry.runtime_data.cursors = CursorCache()
    await client.send_json({"id": 9, **query})
    response = await client.receive_json()
    assert response["error"]["code"] == "cursor_expired"


async def test_cursor_idle_expiry_and_resource_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Expired state and budget failures remain explicit on the public API."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=str(n), timestamp=None, fields={"systolic": n})
            for n in range(2)
        ],
    )
    client = await hass_ws_client(hass)
    query = {
        "type": "custom_records/list_records",
        "record_type": "bp",
        "paginate": True,
        "limit": 1,
    }
    with patch("custom_components.custom_records.pagination.monotonic") as clock:
        clock.return_value = 0
        await client.send_json({"id": 1, **query})
        cursor = (await client.receive_json())["result"]["next_cursor"]
        clock.return_value = 1800
        await client.send_json({"id": 2, **query, "cursor": cursor})
        response = await client.receive_json()
        assert response["error"]["code"] == "cursor_expired"
    entry.runtime_data.cursors = CursorCache(max_bytes=1)
    await client.send_json({"id": 3, **query})
    response = await client.receive_json()
    assert response["error"]["code"] == "pagination_resource_limit"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"start": "not-a-date"}, "invalid_datetime"),
        ({"start": "2026-01-01T00:00:00"}, "invalid_datetime"),
        (
            {"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
            "invalid_time_range",
        ),
        ({"filter": [{"missing": 1}]}, "unknown_filter_field"),
        ({"filter": [{"systolic": "not-a-number"}]}, "invalid_filter_value"),
        ({"filter": [{"systolic": 1, "missing": 2}]}, "invalid_filter_item"),
        ({"limit": 0}, "invalid_format"),
        ({"limit": -1}, "invalid_format"),
        ({"limit": 1.5}, "invalid_format"),
        ({"order": "sideways"}, "invalid_format"),
    ],
)
async def test_paginated_validation_preserves_existing_errors(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    changes: dict[str, Any],
    code: str,
) -> None:
    """Opt-in paging does not bypass normal query validation."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "paginate": True,
            **changes,
        }
    )
    response = await client.receive_json()
    assert response["error"]["code"] == code


async def test_page_failure_retry_and_runtime_reload(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Failed reads preserve the handle, while actual runtime reload invalidates it."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=str(n), timestamp=None, fields={"systolic": n})
            for n in range(3)
        ],
    )
    client = await hass_ws_client(hass)
    query: dict[str, Any] = {
        "type": "custom_records/list_records",
        "record_type": "bp",
        "paginate": True,
        "limit": 1,
    }
    await client.send_json({"id": 1, **query})
    first = (await client.receive_json())["result"]
    query["cursor"] = first["next_cursor"]
    with patch.object(
        entry.runtime_data.storage,
        "async_list_records",
        side_effect=sqlite3.OperationalError("Simulated read failure"),
    ):
        await client.send_json({"id": 2, **query})
        response = await client.receive_json()
        assert response["success"] is False
    await client.send_json({"id": 3, **query})
    retry = (await client.receive_json())["result"]
    assert [row["id"] for row in retry["records"]] == ["1"]
    assert retry["has_more"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await client.send_json({"id": 4, **query})
    response = await client.receive_json()
    assert response["error"]["code"] == "cursor_expired"
    await client.send_json(
        {
            "id": 5,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "paginate": False,
            "limit": 1,
        }
    )
    legacy = (await client.receive_json())["result"]
    assert set(legacy) == {"records"}
    assert legacy["records"] == first["records"]


@pytest.mark.parametrize("order", [None, "asc", "desc"])
@pytest.mark.parametrize("paginate", [False, True])
async def test_explicit_order_independent_of_limit(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    order: str | None,
    *,
    paginate: bool,
) -> None:
    """Both wire modes sort independently of the requested/default page size."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=str(n), timestamp=timestamp, fields={"systolic": n})
            for n in range(3)
        ],
    )
    client = await hass_ws_client(hass)
    query: dict[str, Any] = {
        "type": "custom_records/list_records",
        "record_type": "bp",
        "paginate": paginate,
    }
    if order is not None:
        query["order"] = order
    expected = ["0", "1", "2"] if order == "asc" else ["2", "1", "0"]
    result: dict[str, Any] = {}
    for request_id, extra in enumerate(({}, {"limit": 1}), 1):
        await client.send_json({"id": request_id, **query, **extra})
        result = (await client.receive_json())["result"]
        assert [r["id"] for r in result["records"]] == expected[: extra.get("limit", 3)]
        assert set(result) == (
            {"records", "has_more", "next_cursor"} if paginate else {"records"}
        )
    if paginate:
        cursor = result["next_cursor"]
        await client.send_json(
            {"id": 3, **query, "order": order or "desc", "cursor": cursor}
        )
        continued = (await client.receive_json())["result"]
        assert [r["id"] for r in continued["records"]] == expected[1:]
        assert continued["next_cursor"] is None


async def test_non_paginated_reads_allocate_no_cursor(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Non-paginated responses ignore continuation without allocating cache state."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_import_records(
        "bp",
        [
            ImportRow(id=str(n), timestamp=None, fields={"systolic": n})
            for n in range(2)
        ],
    )
    client = await hass_ws_client(hass)
    with (
        patch.object(
            entry.runtime_data.cursors,
            "issue",
            side_effect=AssertionError("No cursor expected"),
        ),
        patch(
            "custom_components.custom_records.websocket_api.query_scope",
            side_effect=AssertionError("No scope expected"),
        ),
    ):
        await client.send_json(
            {
                "id": 1,
                "type": "custom_records/list_records",
                "record_type": "bp",
                "limit": 1,
            }
        )
        result = (await client.receive_json())["result"]
        assert set(result) == {"records"}
        assert len(result["records"]) == 1
