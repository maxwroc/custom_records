"""Tests for the integration's setup/unload/remove lifecycle."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.custom_records.const import (
    ATTR_ENTRY_ID,
    ATTR_RECORD_TYPE,
    DOMAIN,
    EVENT_RECORDS_UPDATED,
    SUBENTRY_TYPE_RECORD_TYPE,
)
from custom_components.custom_records.models import RecordType
from custom_components.custom_records.store import RecordStorage, SchemaError

from .conftest import BP_RECORD_TYPE, async_setup_entry_with_types, make_source_image

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


def _drop_record_table(db_path: Path, table: str) -> None:
    """Drop a record table to simulate external corruption or data loss."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(f'DROP TABLE "{table}"')


async def test_setup_and_unload_entry(hass: HomeAssistant) -> None:
    """The entry loads, then unloads cleanly without leaking listeners."""
    entry = await async_setup_entry_with_types(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_entry_fires_updated_event_per_record_type(
    hass: HomeAssistant,
) -> None:
    """Setup fires EVENT_RECORDS_UPDATED once per configured record type."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        subentries_data=[
            {
                "data": {
                    k: v for k, v in BP_RECORD_TYPE.items() if k not in ("id", "name")
                },
                "subentry_type": SUBENTRY_TYPE_RECORD_TYPE,
                "title": BP_RECORD_TYPE["name"],
                "unique_id": BP_RECORD_TYPE["id"],
            }
        ],
    )
    entry.add_to_hass(hass)
    captured: list[dict[str, Any]] = []
    legacy_events: list[dict[str, Any]] = []
    assert EVENT_RECORDS_UPDATED == "custom_records_updated"
    hass.bus.async_listen(
        "custom_records_updated", lambda event: captured.append(dict(event.data))
    )
    hass.bus.async_listen(
        "custom_metrics_updated", lambda event: legacy_events.append(dict(event.data))
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert captured == [{ATTR_ENTRY_ID: entry.entry_id, ATTR_RECORD_TYPE: "bp"}]
    assert legacy_events == []


async def test_unload_does_not_delete_data(hass: HomeAssistant) -> None:
    """A plain unload must not delete stored records."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_add_record("bp", {"systolic": 120})

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    reloaded = RecordStorage(hass, entry.entry_id)
    await reloaded.async_load({"bp": RecordType.from_dict(BP_RECORD_TYPE)})
    assert await reloaded.async_record_count("bp") == 1
    await reloaded.async_close()


async def test_remove_entry_deletes_storage(hass: HomeAssistant) -> None:
    """Removing the entry (uninstall) deletes all of its stored records."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_add_record("bp", {"systolic": 120})
    ir.async_create_issue(
        hass,
        DOMAIN,
        "record_count_bp",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="record_count_high",
    )

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    reloaded = RecordStorage(hass, entry.entry_id)
    await reloaded.async_load({"bp": RecordType.from_dict(BP_RECORD_TYPE)})
    assert await reloaded.async_record_count("bp") == 0
    await reloaded.async_close()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "record_count_bp") is None


async def test_legacy_options_migrate_to_subentry_on_reload(
    hass: HomeAssistant,
) -> None:
    """
    Directly poking legacy options (old pre-subentries storage) still works.

    Any entry.options change reloads the entry (_async_update_listener), and
    the migration that runs at the start of every async_setup_entry converts
    leftover options-based record types into subentries - so this still
    works exactly as it did before record types became subentries.
    """
    entry = await async_setup_entry_with_types(hass)
    assert entry.runtime_data.record_types == {}

    hass.config_entries.async_update_entry(
        entry, options={"record_types": [BP_RECORD_TYPE]}
    )
    await hass.async_block_till_done()

    assert "bp" in entry.runtime_data.record_types


async def test_migrates_legacy_options_record_types_on_first_setup(
    hass: HomeAssistant,
) -> None:
    """An entry created the old way (options, no subentries) migrates on setup."""
    entry = MockConfigEntry(
        domain="custom_records",
        data={},
        options={"record_types": [BP_RECORD_TYPE]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert "bp" in entry.runtime_data.record_types
    assert "record_types" not in entry.options
    subentry = next(iter(entry.subentries.values()))
    assert subentry.unique_id == "bp"
    assert subentry.title == "Blood Pressure"
    assert subentry.subentry_type == "record_type"


async def test_missing_table_fails_setup_and_creates_repairs_issue(
    hass: HomeAssistant,
) -> None:
    """Potential database loss is surfaced in Repairs without recreating data."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    assert await hass.config_entries.async_unload(entry.entry_id)
    db_path = Path(
        hass.config.path(".storage", DOMAIN, f"custom_records_{entry.entry_id}.db")
    )
    await hass.async_add_executor_job(_drop_record_table, db_path, "records_bp")

    assert not await hass.config_entries.async_setup(entry.entry_id)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "database_schema_error")
    assert issue is not None
    assert issue.translation_key == "database_schema_error"


async def test_removing_record_type_purges_its_storage_and_media(
    hass: HomeAssistant,
) -> None:
    """Removing a record type's subentry purges its Store file and media dir."""
    entry = await async_setup_entry_with_types(
        hass, [BP_RECORD_TYPE, IMAGE_RECORD_TYPE]
    )
    await entry.runtime_data.storage.async_add_record("bp", {"systolic": 120})
    source = make_source_image(hass, name="cat.jpg")
    filename = await entry.runtime_data.media_store.async_store_image(
        "pets", str(source)
    )
    await entry.runtime_data.storage.async_add_record("pets", {"photo": filename})
    ir.async_create_issue(
        hass,
        DOMAIN,
        "record_count_pets",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="record_count_high",
    )

    pets_media_path = await entry.runtime_data.media_store.async_resolve_image_path(
        "pets", filename
    )
    assert pets_media_path.is_file()

    # Remove the "pets" record type's subentry, keeping "bp".
    pets_subentry_id = next(
        se.subentry_id for se in entry.subentries.values() if se.unique_id == "pets"
    )
    hass.config_entries.async_remove_subentry(entry, pets_subentry_id)
    await hass.async_block_till_done()

    assert "pets" not in entry.runtime_data.record_types
    assert not pets_media_path.is_file()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "record_count_pets") is None

    reloaded = RecordStorage(hass, entry.entry_id)
    try:
        with pytest.raises(SchemaError, match="missing its database table"):
            await reloaded.async_load(
                {
                    "bp": RecordType.from_dict(BP_RECORD_TYPE),
                    "pets": RecordType.from_dict(IMAGE_RECORD_TYPE),
                }
            )
    finally:
        await reloaded.async_close()
