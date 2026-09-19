"""Tests for the custom_records services: add_record, export_records, import_records."""

# pyright: reportArgumentType=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportOperatorIssue=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalSubscript=false

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component

from custom_components.custom_records.const import (
    DOMAIN,
    SERVICE_ADD_RECORD,
    SERVICE_EXPORT_RECORDS,
    SERVICE_IMPORT_RECORDS,
)

from .conftest import (
    BP_RECORD_TYPE,
    async_setup_entry_with_types,
    make_csv_source,
    make_source_image,
    read_text_file,
)

IMAGE_RECORD_TYPE = {
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


async def test_service_registered_even_without_entry(hass: HomeAssistant) -> None:
    """The service is registered at component setup, independent of any entry."""
    assert await async_setup_component(hass, DOMAIN, {})
    for service in ("add_record", "export_records", "import_records"):
        assert hass.services.has_service("custom_records", service)
        assert not hass.services.has_service("custom_metrics", service)


async def test_add_record_happy_path(hass: HomeAssistant) -> None:
    """A valid add_record call stores the record and returns its public shape."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_RECORD,
        {"record_type": "bp", "fields": {"systolic": 120}},
        blocking=True,
        return_response=True,
    )
    assert response["systolic"] == 120.0
    assert "id" in response
    assert "timestamp" in response


async def test_add_record_unknown_record_type(hass: HomeAssistant) -> None:
    """An unknown record_type raises ServiceValidationError."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_RECORD,
            {"record_type": "unknown", "fields": {}},
            blocking=True,
        )


async def test_add_record_missing_required_field(hass: HomeAssistant) -> None:
    """A missing required field raises ServiceValidationError."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_RECORD,
            {"record_type": "bp", "fields": {}},
            blocking=True,
        )


async def test_add_record_not_set_up(hass: HomeAssistant) -> None:
    """Calling the service with no loaded entry raises ServiceValidationError."""
    assert await async_setup_component(hass, DOMAIN, {})

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_RECORD,
            {"record_type": "bp", "fields": {}},
            blocking=True,
        )


async def test_add_record_stores_image_reference_not_raw_path(
    hass: HomeAssistant,
) -> None:
    """An IMAGE field's filesystem path is replaced with a media_source link."""
    await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])
    source_file = make_source_image(hass, name="cat.jpg")

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_RECORD,
        {"record_type": "pets", "fields": {"photo": str(source_file)}},
        blocking=True,
        return_response=True,
    )

    expected_media_source = f"media-source://custom_records/pets/{response['id']}/photo"
    assert response["photo"] == {"media_source": expected_media_source}


async def test_add_record_rejects_missing_image_path(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A non-existent image path raises ServiceValidationError, not a raw crash."""
    await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_RECORD,
            {"record_type": "pets", "fields": {"photo": str(tmp_path / "missing.jpg")}},
            blocking=True,
        )


async def test_export_records_returns_csv_text_full_by_default(
    hass: HomeAssistant,
) -> None:
    """With no `path`, export_records returns the CSV text, id column included."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_RECORD,
        {"record_type": "bp", "fields": {"systolic": 120}},
        blocking=True,
    )

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_RECORDS,
        {"record_type": "bp"},
        blocking=True,
        return_response=True,
    )

    lines = response["csv"].splitlines()
    assert lines[0] == "id,timestamp,systolic"
    assert len(lines) == 2


async def test_export_records_include_id_false(hass: HomeAssistant) -> None:
    """include_id=False omits the id column."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_RECORD,
        {"record_type": "bp", "fields": {"systolic": 120}},
        blocking=True,
    )

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_RECORDS,
        {"record_type": "bp", "include_id": False},
        blocking=True,
        return_response=True,
    )

    assert response["csv"].splitlines()[0] == "timestamp,systolic"


async def test_export_records_unknown_record_type(hass: HomeAssistant) -> None:
    """An unknown record_type raises ServiceValidationError."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_RECORDS,
            {"record_type": "unknown"},
            blocking=True,
        )


async def test_export_records_to_path_writes_file(hass: HomeAssistant) -> None:
    """Given a `path`, export_records writes the CSV there and returns the path."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_RECORD,
        {"record_type": "bp", "fields": {"systolic": 120}},
        blocking=True,
    )
    target_path = make_csv_source(hass, "placeholder", name="export.csv")

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_RECORDS,
        {"record_type": "bp", "path": str(target_path)},
        blocking=True,
        return_response=True,
    )

    assert read_text_file(response["path"]).startswith("id,timestamp,systolic")


async def test_export_records_path_outside_allowed_root_rejected(
    hass: HomeAssistant,
) -> None:
    """A `path` outside the allow-listed roots is rejected."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_RECORDS,
            {"record_type": "bp", "path": "/etc/export.csv"},
            blocking=True,
        )


async def test_import_records_from_content(hass: HomeAssistant) -> None:
    """import_records parses inline `content` and stores the resulting rows."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    csv_text = "id,timestamp,systolic\n,2026-01-01T10:00:00+00:00,120\n"

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_RECORDS,
        {"record_type": "bp", "content": csv_text},
        blocking=True,
        return_response=True,
    )

    assert response["imported"] == 1
    assert response["skipped_duplicate"] == 0
    assert response["errors"] == []


async def test_import_records_from_path(hass: HomeAssistant) -> None:
    """import_records reads a CSV file from an allow-listed `path`."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    source_path = make_csv_source(
        hass, "id,timestamp,systolic\n,2026-01-01T10:00:00+00:00,120\n"
    )

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_RECORDS,
        {"record_type": "bp", "path": str(source_path)},
        blocking=True,
        return_response=True,
    )

    assert response["imported"] == 1


async def test_import_records_path_outside_allowed_root_rejected(
    hass: HomeAssistant,
) -> None:
    """A `path` outside the allow-listed roots is rejected."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_RECORDS,
            {"record_type": "bp", "path": "/etc/import.csv"},
            blocking=True,
        )


async def test_import_records_requires_exactly_one_of_path_content(
    hass: HomeAssistant,
) -> None:
    """Neither or both of `path`/`content` given raises ServiceValidationError."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_RECORDS,
            {"record_type": "bp"},
            blocking=True,
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_RECORDS,
            {"record_type": "bp", "path": "/config/a.csv", "content": "id\n"},
            blocking=True,
        )
