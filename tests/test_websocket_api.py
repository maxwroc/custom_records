"""Tests for the custom_records WebSocket API commands."""

# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import FormData
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.custom_records.const import MAX_LIST_RECORDS_LIMIT

from .conftest import BP_RECORD_TYPE, async_setup_entry_with_types, make_source_image

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp.test_utils import TestClient
    from pytest_homeassistant_custom_component.typing import (
        ClientSessionGenerator,
        WebSocketGenerator,
    )


@pytest.mark.parametrize(
    "command",
    [
        "list_record_types",
        "list_records",
        "aggregate_records",
        "get_field_stats",
        "histogram_records",
        "compare_periods",
        "add_record",
        "delete_record",
        "validate_image_path",
    ],
)
async def test_legacy_namespace_is_not_registered(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, command: str
) -> None:
    """Fresh installations do not register compatibility aliases."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": f"custom_metrics/{command}"})
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unknown_command"


async def test_list_record_types(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """list_record_types returns the configured record types."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "custom_records/list_record_types"})
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["record_types"][0]["id"] == "bp"


async def test_add_list_delete_record(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """add_record/list_records/delete_record work end-to-end."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/add_record",
            "record_type": "bp",
            "fields": {"systolic": 120},
        }
    )
    response = await client.receive_json()
    assert response["success"]
    record_id = response["result"]["record"]["id"]

    await client.send_json(
        {"id": 2, "type": "custom_records/list_records", "record_type": "bp"}
    )
    response = await client.receive_json()
    assert len(response["result"]["records"]) == 1

    await client.send_json(
        {
            "id": 3,
            "type": "custom_records/delete_record",
            "record_type": "bp",
            "record_id": record_id,
        }
    )
    response = await client.receive_json()
    assert response["result"]["deleted"] is True


async def test_add_record_rejects_invalid_timestamp(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """An explicit malformed timestamp is not silently replaced with now."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/add_record",
            "record_type": "bp",
            "fields": {"systolic": 120},
            "timestamp": "not-a-date",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_datetime"


async def test_unknown_record_type_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Unknown record_type ids return an error, not a crash."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {"id": 1, "type": "custom_records/list_records", "record_type": "nope"}
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unknown_record_type"


async def test_delete_missing_record_returns_not_found(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Deleting a non-existent record id returns a not_found error."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/delete_record",
            "record_type": "bp",
            "record_id": "missing",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_validate_image_path_for_existing_file(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """An existing, allowed-extension image path validates as valid."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    source_file = make_source_image(hass, name="cat.jpg")
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/validate_image_path",
            "path": str(source_file),
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"] == {"valid": True, "error": None}


async def test_validate_image_path_for_missing_file(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, tmp_path: Path
) -> None:
    """A non-existent path validates as invalid, with an explanatory error."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/validate_image_path",
            "path": str(tmp_path / "missing.jpg"),
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["valid"] is False
    assert response["result"]["error"]


async def test_add_record_invalid_image_path_returns_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, tmp_path: Path
) -> None:
    """add_record with a non-existent image path returns an invalid_image error."""
    image_record_type = {
        "id": "pets",
        "name": "Pets",
        "fields": [
            {
                "key": "photo",
                "label": "Photo",
                "type": "image",
                "required": False,
                "unit": None,
                "default": None,
                "options": None,
            },
        ],
        "timestamp_field": "timestamp",
        "retention_days": None,
        "max_records": None,
        "warn_at": None,
    }
    await async_setup_entry_with_types(hass, [image_record_type])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/add_record",
            "record_type": "pets",
            "fields": {"photo": str(tmp_path / "missing.jpg")},
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_image"


async def test_add_record_and_list_records_expose_media_source(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """add_record/list_records return a media_source link for stored images."""
    image_record_type = {
        "id": "pets",
        "name": "Pets",
        "fields": [
            {
                "key": "photo",
                "label": "Photo",
                "type": "image",
                "required": False,
                "unit": None,
                "default": None,
                "options": None,
            },
        ],
        "timestamp_field": "timestamp",
        "retention_days": None,
        "max_records": None,
        "warn_at": None,
    }
    await async_setup_entry_with_types(hass, [image_record_type])
    source_file = make_source_image(hass, name="cat.jpg")
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/add_record",
            "record_type": "pets",
            "fields": {"photo": str(source_file)},
        }
    )
    response = await client.receive_json()
    assert response["success"]
    record_id = response["result"]["record"]["id"]
    expected_media_source = f"media-source://custom_records/pets/{record_id}/photo"
    assert response["result"]["record"]["photo"] == {
        "media_source": expected_media_source
    }

    await client.send_json(
        {"id": 2, "type": "custom_records/list_records", "record_type": "pets"}
    )
    response = await client.receive_json()
    assert response["success"]
    records = response["result"]["records"]
    assert len(records) == 1
    assert records[0]["photo"] == {"media_source": expected_media_source}


async def _upload_file(client: TestClient, content: bytes, filename: str) -> str:
    """Upload a file via the standard /api/file_upload endpoint; return its file_id."""
    form = FormData()
    form.add_field("file", content, filename=filename, content_type="image/jpeg")
    resp = await client.post("/api/file_upload", data=form)
    assert resp.status == 200
    return (await resp.json())["file_id"]


async def test_add_record_with_uploaded_file_id(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    """add_record accepts a {"file_id": ...} image value from an uploaded file."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})
    image_record_type = {
        "id": "pets",
        "name": "Pets",
        "fields": [
            {
                "key": "photo",
                "label": "Photo",
                "type": "image",
                "required": False,
                "unit": None,
                "default": None,
                "options": None,
            },
        ],
        "timestamp_field": "timestamp",
        "retention_days": None,
        "max_records": None,
        "warn_at": None,
    }
    await async_setup_entry_with_types(hass, [image_record_type])
    http_client = await hass_client()
    file_id = await _upload_file(http_client, b"fake-image-bytes", "dog.jpg")
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "custom_records/add_record",
            "record_type": "pets",
            "fields": {"photo": {"file_id": file_id}},
        }
    )
    response = await ws_client.receive_json()

    assert response["success"]
    record_id = response["result"]["record"]["id"]
    assert response["result"]["record"]["photo"] == {
        "media_source": f"media-source://custom_records/pets/{record_id}/photo"
    }


async def test_add_record_unknown_file_id_returns_invalid_image(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """add_record with an unknown/expired file_id returns an invalid_image error."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})
    image_record_type = {
        "id": "pets",
        "name": "Pets",
        "fields": [
            {
                "key": "photo",
                "label": "Photo",
                "type": "image",
                "required": False,
                "unit": None,
                "default": None,
                "options": None,
            },
        ],
        "timestamp_field": "timestamp",
        "retention_days": None,
        "max_records": None,
        "warn_at": None,
    }
    await async_setup_entry_with_types(hass, [image_record_type])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/add_record",
            "record_type": "pets",
            "fields": {"photo": {"file_id": "unknown-file-id"}},
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_image"


async def test_list_records_limit_sorts_newest_first(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """An explicit limit caps the result and orders it newest-first."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    now = dt_util.utcnow()
    for i in range(5):
        await storage.async_add_record(
            "bp", {"systolic": i}, timestamp=now + timedelta(seconds=i)
        )

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "limit": 2,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    records = response["result"]["records"]
    assert [r["systolic"] for r in records] == [4, 3]


async def test_list_records_rejects_invalid_datetime(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Malformed time filters return an error instead of disabling filtering."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "start": "not-a-date",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_datetime"


async def test_list_records_rejects_reversed_time_range(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A start after end is rejected instead of returning a misleading empty list."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "start": "2026-02-01T00:00:00+00:00",
            "end": "2026-01-01T00:00:00+00:00",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_time_range"


async def test_list_records_without_limit_is_still_capped(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Even with no explicit limit, results are capped at MAX_LIST_RECORDS_LIMIT."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    for i in range(MAX_LIST_RECORDS_LIMIT + 5):
        await storage.async_add_record("bp", {"systolic": i})

    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "custom_records/list_records", "record_type": "bp"}
    )
    response = await client.receive_json()

    assert response["success"]
    assert len(response["result"]["records"]) == MAX_LIST_RECORDS_LIMIT


async def test_list_records_limit_above_cap_is_clamped(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A requested limit above the server cap is clamped, not rejected."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    for i in range(MAX_LIST_RECORDS_LIMIT + 5):
        await storage.async_add_record("bp", {"systolic": i})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "bp",
            "limit": MAX_LIST_RECORDS_LIMIT * 10,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert len(response["result"]["records"]) == MAX_LIST_RECORDS_LIMIT


# Record type used by the `filter` tests below - a mix of field types so each
# operator-compatibility rejection can be exercised (label=text, photo=image).
_FILTER_RECORD_TYPE: dict[str, Any] = {
    "id": "widgets",
    "name": "Widgets",
    "fields": [
        {
            "key": "count",
            "label": "Count",
            "type": "number",
            "required": False,
            "unit": None,
            "default": None,
            "options": None,
        },
        {
            "key": "label",
            "label": "Label",
            "type": "text",
            "required": False,
            "unit": None,
            "default": None,
            "options": None,
        },
        {
            "key": "photo",
            "label": "Photo",
            "type": "image",
            "required": False,
            "unit": None,
            "default": None,
            "options": None,
        },
    ],
    "timestamp_field": "timestamp",
    "retention_days": None,
    "max_records": None,
    "warn_at": None,
}


async def test_list_records_filter_happy_path(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A `filter` list only returns records matching every item (AND-combined)."""
    entry = await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    storage = entry.runtime_data.storage
    for count in (50, 100, 150):
        await storage.async_add_record("widgets", {"count": count, "label": "a"})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "widgets",
            "filter": [{"count": "> 75"}],
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert sorted(r["count"] for r in response["result"]["records"]) == [100, 150]


async def test_list_records_filter_unknown_field_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Filtering on a field that doesn't exist on the record type is an error."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "widgets",
            "filter": [{"nope": 1}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unknown_filter_field"


async def test_list_records_filter_image_field_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Filtering on an IMAGE-type field is rejected - it's an internal object."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "widgets",
            "filter": [{"photo": "x"}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unsupported_filter_field"


async def test_list_records_filter_unsupported_operator_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A comparison operator not valid for the field's type is a clear error."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "widgets",
            "filter": [{"label": "> a"}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unsupported_filter_operator"


async def test_list_records_filter_invalid_value_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A filter value that doesn't coerce to the field's type is a clear error."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "widgets",
            "filter": [{"count": "> notanumber"}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_filter_value"


async def test_list_records_filter_invalid_item_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A `filter` item that isn't a single-key map is rejected as a whole."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/list_records",
            "record_type": "widgets",
            "filter": [{"count": 1, "label": "a"}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_filter_item"


# -- aggregate_records (plan_sql.md Phase 4) ---------------------------------


async def test_aggregate_records_sum_by_day(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """sum/day buckets records by UTC calendar day, ascending, sparse."""
    entry = await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T10:00:00+00:00")
    day3 = dt_util.parse_datetime("2026-01-03T10:00:00+00:00")
    await storage.async_add_record("widgets", {"count": 10}, timestamp=day1)
    await storage.async_add_record("widgets", {"count": 20}, timestamp=day1)
    await storage.async_add_record("widgets", {"count": 5}, timestamp=day3)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "widgets",
            "op": "sum",
            "bucket": "day",
            "field": "count",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    buckets = response["result"]["buckets"]
    assert [b["value"] for b in buckets] == [30.0, 5.0]
    assert [b["count"] for b in buckets] == [2, 1]
    assert buckets[0]["start"] == "2026-01-01T00:00:00+00:00"
    assert buckets[1]["start"] == "2026-01-03T00:00:00+00:00"


async def test_aggregate_records_count_forbids_field(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`op: count` must not accept a `field` parameter."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "widgets",
            "op": "count",
            "bucket": "day",
            "field": "count",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "field_forbidden"


async def test_aggregate_records_numeric_op_requires_field(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`op: sum` (etc.) requires a `field` parameter."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "widgets",
            "op": "sum",
            "bucket": "day",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "field_required"


async def test_aggregate_records_count_op(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`op: count` counts records per bucket, regardless of field values."""
    entry = await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T10:00:00+00:00")
    for _ in range(3):
        await storage.async_add_record("widgets", {}, timestamp=day1)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "widgets",
            "op": "count",
            "bucket": "day",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    buckets = response["result"]["buckets"]
    assert buckets == [{"start": "2026-01-01T00:00:00+00:00", "value": 3, "count": 3}]


async def test_aggregate_records_with_filter(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`filter` narrows aggregation to matching rows, same as list_records."""
    entry = await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T10:00:00+00:00")
    await storage.async_add_record(
        "widgets", {"count": 10, "label": "a"}, timestamp=day1
    )
    await storage.async_add_record(
        "widgets", {"count": 999, "label": "b"}, timestamp=day1
    )

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "widgets",
            "op": "sum",
            "bucket": "day",
            "field": "count",
            "filter": [{"label": "a"}],
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["buckets"] == [
        {"start": "2026-01-01T00:00:00+00:00", "value": 10.0, "count": 1}
    ]


async def test_aggregate_records_apexcharts_format(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`format: apexcharts` returns an ApexCharts-ready series."""
    entry = await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    await storage.async_add_record("widgets", {"count": 42}, timestamp=day1)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "widgets",
            "op": "sum",
            "bucket": "day",
            "field": "count",
            "format": "apexcharts",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    series = response["result"]["series"]
    assert series[0]["name"] == "Count"
    assert series[0]["data"] == [{"x": int(day1.timestamp() * 1000), "y": 42.0}]


async def test_aggregate_records_unknown_field_error(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """An unknown numeric field for sum/avg/min/max is a clear error."""
    await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "widgets",
            "op": "sum",
            "bucket": "day",
            "field": "nope",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unknown_field"


# -- aggregate_records v2: bucket granularity/group_by/metrics/cumulative ----

_GROUP_RECORD_TYPE: dict[str, Any] = {
    "id": "expenses",
    "name": "Expenses",
    "fields": [
        {
            "key": "amount",
            "label": "Amount",
            "type": "number",
            "required": False,
            "unit": None,
            "default": None,
            "options": None,
        },
        {
            "key": "category",
            "label": "Category",
            "type": "single_select",
            "required": False,
            "unit": None,
            "default": None,
            "options": ["food", "fuel"],
        },
        {
            "key": "tags",
            "label": "Tags",
            "type": "multi_select",
            "required": False,
            "unit": None,
            "default": None,
            "options": ["work", "personal", "urgent"],
        },
    ],
    "timestamp_field": "timestamp",
    "retention_days": None,
    "max_records": None,
    "warn_at": None,
}


async def test_aggregate_records_hour_bucket(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`bucket: hour` groups by the UTC calendar hour."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    t1 = dt_util.parse_datetime("2026-01-01T10:15:00+00:00")
    t2 = dt_util.parse_datetime("2026-01-01T10:45:00+00:00")
    t3 = dt_util.parse_datetime("2026-01-01T11:05:00+00:00")
    await storage.async_add_record("expenses", {"amount": 10}, timestamp=t1)
    await storage.async_add_record("expenses", {"amount": 20}, timestamp=t2)
    await storage.async_add_record("expenses", {"amount": 5}, timestamp=t3)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "bucket": "hour",
            "field": "amount",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    buckets = response["result"]["buckets"]
    assert [b["value"] for b in buckets] == [30.0, 5.0]
    assert buckets[0]["start"] == "2026-01-01T10:00:00+00:00"
    assert buckets[1]["start"] == "2026-01-01T11:00:00+00:00"


async def test_aggregate_records_custom_minute_bucket(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`bucket: "15m"` groups into fixed 15-minute, epoch-aligned buckets."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    t1 = dt_util.parse_datetime("2026-01-01T10:02:00+00:00")
    t2 = dt_util.parse_datetime("2026-01-01T10:10:00+00:00")
    t3 = dt_util.parse_datetime("2026-01-01T10:20:00+00:00")
    await storage.async_add_record("expenses", {"amount": 10}, timestamp=t1)
    await storage.async_add_record("expenses", {"amount": 20}, timestamp=t2)
    await storage.async_add_record("expenses", {"amount": 5}, timestamp=t3)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "bucket": "15m",
            "field": "amount",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    buckets = response["result"]["buckets"]
    assert [b["value"] for b in buckets] == [30.0, 5.0]


async def test_aggregate_records_auto_bucket_requires_start_end(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`bucket: "auto"` without both start/end is a clear error."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "count",
            "bucket": "auto",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_bucket"


async def test_aggregate_records_auto_bucket_picks_day_for_medium_range(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`bucket: "auto"` picks a day bucket for a multi-week range."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T10:00:00+00:00")
    await storage.async_add_record("expenses", {"amount": 10}, timestamp=day1)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "count",
            "bucket": "auto",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-10T00:00:00+00:00",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["buckets"][0]["start"] == "2026-01-01T00:00:00+00:00"


async def test_aggregate_records_invalid_bucket_string(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A nonsensical bucket string is a clear error, not a silent fallback."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "count",
            "bucket": "fortnight",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_bucket"


async def test_aggregate_records_group_by_single_select(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`group_by` on a single_select field groups by its plain value."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record("expenses", {"amount": 10, "category": "food"})
    await storage.async_add_record("expenses", {"amount": 20, "category": "food"})
    await storage.async_add_record("expenses", {"amount": 5, "category": "fuel"})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "group_by": "category",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    groups = {g["group"]: g["value"] for g in response["result"]["groups"]}
    assert groups == {"food": 30.0, "fuel": 5.0}


async def test_aggregate_records_group_by_multi_select_explodes(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`group_by` on a multi_select field explodes into one group per value."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record(
        "expenses", {"amount": 10, "tags": ["work", "urgent"]}
    )
    await storage.async_add_record("expenses", {"amount": 20, "tags": ["work"]})
    await storage.async_add_record("expenses", {"amount": 5})  # no tags at all

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "group_by": "tags",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    groups = {g["group"]: g["value"] for g in response["result"]["groups"]}
    assert groups == {"work": 30.0, "urgent": 10.0}


async def test_aggregate_records_group_by_with_bucket_apexcharts(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """group_by + bucket + apexcharts returns one named series per group."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    await storage.async_add_record(
        "expenses", {"amount": 10, "category": "food"}, timestamp=day1
    )
    await storage.async_add_record(
        "expenses", {"amount": 5, "category": "fuel"}, timestamp=day1
    )

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "bucket": "day",
            "group_by": "category",
            "format": "apexcharts",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    series_by_name = {s["name"]: s["data"] for s in response["result"]["series"]}
    assert series_by_name["food"] == [{"x": int(day1.timestamp() * 1000), "y": 10.0}]
    assert series_by_name["fuel"] == [{"x": int(day1.timestamp() * 1000), "y": 5.0}]


async def test_aggregate_records_group_by_only_apexcharts_categorical(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """group_by without bucket + apexcharts returns a labels/series pair."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record("expenses", {"amount": 10, "category": "food"})
    await storage.async_add_record("expenses", {"amount": 5, "category": "fuel"})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "group_by": "category",
            "format": "apexcharts",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    result = response["result"]
    assert set(result["labels"]) == {"food", "fuel"}
    assert set(result["series"]) == {10.0, 5.0}


async def test_aggregate_records_no_bucket_no_group_single_value(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Omitting both bucket and group_by returns one overall summary value."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record("expenses", {"amount": 10})
    await storage.async_add_record("expenses", {"amount": 20})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"] == {"value": 30.0, "count": 2}


async def test_aggregate_records_format_apexcharts_requires_bucket_or_group(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Apexcharts format with neither bucket nor group_by is rejected."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "format": "apexcharts",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unsupported_format"


async def test_aggregate_records_metrics_multi_series(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`metrics` returns multiple named series/values from one call."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    await storage.async_add_record("expenses", {"amount": 10}, timestamp=day1)
    await storage.async_add_record("expenses", {"amount": 30}, timestamp=day1)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "bucket": "day",
            "metrics": [
                {"op": "sum", "field": "amount", "name": "total"},
                {"op": "avg", "field": "amount", "name": "average"},
            ],
        }
    )
    response = await client.receive_json()

    assert response["success"]
    bucket = response["result"]["buckets"][0]
    assert bucket["values"] == {"total": 40.0, "average": 20.0}
    assert bucket["counts"] == {"total": 2, "average": 2}


async def test_aggregate_records_legacy_metrics_conflict(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Passing both `op` and `metrics` in one call is rejected."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "bucket": "day",
            "metrics": [{"op": "count"}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "legacy_metrics_conflict"


async def test_aggregate_records_op_or_metrics_required(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Neither `op` nor `metrics` given is a clear error."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "bucket": "day",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "op_or_metrics_required"


async def test_aggregate_records_group_by_metrics_conflict(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`group_by` combined with `metrics` is rejected."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "bucket": "day",
            "group_by": "category",
            "metrics": [{"op": "count"}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "group_by_metrics_conflict"


async def test_aggregate_records_too_many_metrics(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A `metrics` list beyond the max entry count is rejected."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "bucket": "day",
            "metrics": [{"op": "count"} for _ in range(11)],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "too_many_metrics"


async def test_aggregate_records_duplicate_metric_name(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Two `metrics` entries resolving to the same name is rejected."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "bucket": "day",
            "metrics": [{"op": "count"}, {"op": "count"}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "duplicate_metric_name"


async def test_aggregate_records_invalid_metric_name(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A metric name must be a non-empty string."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "bucket": "day",
            "metrics": [{"op": "count", "name": []}],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_metrics"


async def test_aggregate_records_unknown_group_by_field(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """An unknown `group_by` field is a clear error."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "count",
            "bucket": "day",
            "group_by": "nope",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unknown_group_by_field"


async def test_aggregate_records_cumulative_sum(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`cumulative` turns each bucket's value into a running total."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T10:00:00+00:00")
    day2 = dt_util.parse_datetime("2026-01-02T10:00:00+00:00")
    day3 = dt_util.parse_datetime("2026-01-03T10:00:00+00:00")
    await storage.async_add_record("expenses", {"amount": 10}, timestamp=day1)
    await storage.async_add_record("expenses", {"amount": 20}, timestamp=day2)
    await storage.async_add_record("expenses", {"amount": 5}, timestamp=day3)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "bucket": "day",
            "cumulative": True,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    values = [b["value"] for b in response["result"]["buckets"]]
    assert values == [10.0, 30.0, 35.0]


async def test_aggregate_records_cumulative_avg_is_sample_weighted(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Cumulative `avg` is a running sample-weighted mean, not avg-of-avgs."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T10:00:00+00:00")
    day2 = dt_util.parse_datetime("2026-01-02T10:00:00+00:00")
    await storage.async_add_record("expenses", {"amount": 10}, timestamp=day1)
    await storage.async_add_record("expenses", {"amount": 20}, timestamp=day2)
    await storage.async_add_record("expenses", {"amount": 30}, timestamp=day2)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "avg",
            "field": "amount",
            "bucket": "day",
            "cumulative": True,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    values = [b["value"] for b in response["result"]["buckets"]]
    # day1: avg=10 (n=1). day2 alone: avg=25 (n=2). Cumulative day2 must be the
    # weighted mean over all 3 samples (10+20+30)/3 = 20, NOT avg(10, 25)=17.5.
    assert values == [10.0, 20.0]


async def test_aggregate_records_cumulative_requires_bucket(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`cumulative` without `bucket` is a clear error."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/aggregate_records",
            "record_type": "expenses",
            "op": "count",
            "cumulative": True,
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "cumulative_requires_bucket"


# -- get_field_stats ----------------------------------------------------------


async def test_get_field_stats_default_all_stats(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """With no `stats` param, every supported stat is returned."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    day2 = dt_util.parse_datetime("2026-01-02T00:00:00+00:00")
    await storage.async_add_record("expenses", {"amount": 10}, timestamp=day1)
    await storage.async_add_record("expenses", {"amount": 30}, timestamp=day2)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/get_field_stats",
            "record_type": "expenses",
            "field": "amount",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    stats = response["result"]["stats"]
    assert stats == {
        "first": 10.0,
        "last": 30.0,
        "min": 10.0,
        "max": 30.0,
        "sum": 40.0,
        "avg": 20.0,
        "count": 2,
    }


async def test_get_field_stats_subset(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A `stats` subset returns only the requested keys."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record("expenses", {"amount": 10})
    await storage.async_add_record("expenses", {"amount": 30})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/get_field_stats",
            "record_type": "expenses",
            "field": "amount",
            "stats": ["min", "max"],
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["stats"] == {"min": 10.0, "max": 30.0}


async def test_get_field_stats_first_last_null_field(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """first/last reflect the literal first/last record, even if unset."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    day1 = dt_util.parse_datetime("2026-01-01T00:00:00+00:00")
    day2 = dt_util.parse_datetime("2026-01-02T00:00:00+00:00")
    await storage.async_add_record("expenses", {}, timestamp=day1)
    await storage.async_add_record("expenses", {"amount": 30}, timestamp=day2)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/get_field_stats",
            "record_type": "expenses",
            "field": "amount",
            "stats": ["first", "last"],
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["stats"] == {"first": None, "last": 30.0}


async def test_get_field_stats_unsupported_field(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A non-numeric field is rejected."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/get_field_stats",
            "record_type": "expenses",
            "field": "category",
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unsupported_field"


# -- histogram_records --------------------------------------------------------


async def test_histogram_records_default_bin_count(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Without bin_count/bin_width, 10 equal-width bins span min..max."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    for value in (0, 10, 20, 100):
        await storage.async_add_record("expenses", {"amount": value})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/histogram_records",
            "record_type": "expenses",
            "field": "amount",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    result = response["result"]
    assert len(result["bins"]) == 10
    assert result["min"] == 0.0
    assert result["max"] == 100.0
    assert sum(b["count"] for b in result["bins"]) == 4
    # The max value (100) must land in the LAST bin, not spill into a phantom
    # 11th bin.
    assert result["bins"][-1]["count"] == 1


async def test_histogram_records_bin_width(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`bin_width` derives the bin count from the data range."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    for value in (0, 5, 15, 25):
        await storage.async_add_record("expenses", {"amount": value})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/histogram_records",
            "record_type": "expenses",
            "field": "amount",
            "bin_width": 10,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    result = response["result"]
    assert result["bin_width"] == 10.0
    assert len(result["bins"]) == 3
    assert [b["count"] for b in result["bins"]] == [2, 1, 1]


async def test_histogram_records_min_max_override(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Explicit `min`/`max` skip the auto-range query and set the bin edges."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    for value in (-5, 5, 15):
        await storage.async_add_record("expenses", {"amount": value})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/histogram_records",
            "record_type": "expenses",
            "field": "amount",
            "bin_count": 2,
            "min": 0,
            "max": 10,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    result = response["result"]
    assert result["min"] == 0.0
    assert result["max"] == 10.0
    assert len(result["bins"]) == 2
    assert [item["count"] for item in result["bins"]] == [0, 1]


async def test_histogram_records_invalid_override_range(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Explicit histogram bounds must define an increasing range."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/histogram_records",
            "record_type": "expenses",
            "field": "amount",
            "min": 10,
            "max": 0,
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_histogram_range"


async def test_histogram_records_bin_count_width_conflict(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Providing both `bin_count` and `bin_width` is rejected."""
    await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/histogram_records",
            "record_type": "expenses",
            "field": "amount",
            "bin_count": 5,
            "bin_width": 10,
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "bin_count_width_conflict"


async def test_histogram_records_too_many_bins(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A `bin_width` that resolves to too many bins is rejected."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record("expenses", {"amount": 0})
    await storage.async_add_record("expenses", {"amount": 1000})

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/histogram_records",
            "record_type": "expenses",
            "field": "amount",
            "bin_width": 1,
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "too_many_bins"


# -- compare_periods -----------------------------------------------------------


async def test_compare_periods_basic_delta(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """current/previous/delta are computed from two explicit periods."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    jan = dt_util.parse_datetime("2026-01-15T00:00:00+00:00")
    feb = dt_util.parse_datetime("2026-02-15T00:00:00+00:00")
    await storage.async_add_record("expenses", {"amount": 100}, timestamp=jan)
    await storage.async_add_record("expenses", {"amount": 150}, timestamp=feb)

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/compare_periods",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "current": {
                "start": "2026-02-01T00:00:00+00:00",
                "end": "2026-03-01T00:00:00+00:00",
            },
            "previous": {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-02-01T00:00:00+00:00",
            },
        }
    )
    response = await client.receive_json()

    assert response["success"]
    result = response["result"]
    assert result["current"] == {"value": 150.0, "count": 1}
    assert result["previous"] == {"value": 100.0, "count": 1}
    assert result["delta"] == 50.0
    assert result["delta_pct"] == 50.0


async def test_compare_periods_auto_derives_previous(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Omitting `previous` auto-derives the immediately preceding equal period."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record(
        "expenses",
        {"amount": 5},
        timestamp=dt_util.parse_datetime("2026-01-05T00:00:00+00:00"),
    )
    await storage.async_add_record(
        "expenses",
        {"amount": 20},
        timestamp=dt_util.parse_datetime("2026-01-15T00:00:00+00:00"),
    )
    await storage.async_add_record(
        "expenses",
        {"amount": 100},
        timestamp=dt_util.parse_datetime("2026-01-10T00:00:00+00:00"),
    )

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/compare_periods",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "current": {
                "start": "2026-01-10T00:00:00+00:00",
                "end": "2026-01-20T00:00:00+00:00",
            },
        }
    )
    response = await client.receive_json()

    assert response["success"]
    result = response["result"]
    assert result["current"] == {"value": 120.0, "count": 2}
    assert result["previous"] == {"value": 5.0, "count": 1}


async def test_compare_periods_with_group_by(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`group_by` returns per-group current/previous/deltas."""
    entry = await async_setup_entry_with_types(hass, [_GROUP_RECORD_TYPE])
    storage = entry.runtime_data.storage
    await storage.async_add_record(
        "expenses",
        {"amount": 10, "category": "food"},
        timestamp=dt_util.parse_datetime("2026-01-15T00:00:00+00:00"),
    )
    await storage.async_add_record(
        "expenses",
        {"amount": 40, "category": "food"},
        timestamp=dt_util.parse_datetime("2026-02-15T00:00:00+00:00"),
    )

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/compare_periods",
            "record_type": "expenses",
            "op": "sum",
            "field": "amount",
            "group_by": "category",
            "current": {
                "start": "2026-02-01T00:00:00+00:00",
                "end": "2026-03-01T00:00:00+00:00",
            },
            "previous": {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-02-01T00:00:00+00:00",
            },
        }
    )
    response = await client.receive_json()

    assert response["success"]
    result = response["result"]
    deltas = {d["group"]: d["delta"] for d in result["deltas"]}
    assert deltas == {"food": 30.0}


async def test_compare_periods_group_by_image(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Image group values (bare filename strings) are compared correctly."""
    entry = await async_setup_entry_with_types(hass, [_FILTER_RECORD_TYPE])
    storage = entry.runtime_data.storage
    image_filename = "receipt.jpg"
    await storage.async_add_record(
        "widgets",
        {"count": 10, "photo": image_filename},
        timestamp=dt_util.parse_datetime("2026-01-15T00:00:00+00:00"),
    )
    await storage.async_add_record(
        "widgets",
        {"count": 40, "photo": image_filename},
        timestamp=dt_util.parse_datetime("2026-02-15T00:00:00+00:00"),
    )

    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "custom_records/compare_periods",
            "record_type": "widgets",
            "op": "sum",
            "field": "count",
            "group_by": "photo",
            "current": {
                "start": "2026-02-01T00:00:00+00:00",
                "end": "2026-03-01T00:00:00+00:00",
            },
            "previous": {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-31T23:59:59.999999+00:00",
            },
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["deltas"] == [
        {"group": image_filename, "delta": 30.0, "delta_pct": 300.0}
    ]
