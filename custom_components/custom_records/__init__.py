"""
Custom Records.

A Home Assistant integration for recording user-defined metrics (blood
pressure, fuel costs, doorbell snapshots, etc.) via a service call, exposed to
custom Lovelace cards through a small WebSocket API.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ATTR_ENTRY_ID,
    ATTR_RECORD_TYPE,
    CONF_RECORD_TYPES,
    DOMAIN,
    EVENT_RECORDS_UPDATED,
    LOGGER,
    SUBENTRY_TYPE_RECORD_TYPE,
)
from .export_view import async_register_export_view
from .frontend import async_register_frontend
from .media_store import MediaStore, async_register_media_view
from .models import RecordType
from .runtime_data import CustomRecordsRuntimeData
from .services import async_setup_services
from .store import RecordStorage, SchemaError
from .websocket_api import async_setup_websocket_api

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .runtime_data import CustomRecordsConfigEntry

PURGE_INTERVAL = timedelta(hours=24)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up global (hass-wide) services, WebSocket commands, and media view."""
    del config
    async_setup_services(hass)
    async_setup_websocket_api(hass)
    async_register_media_view(hass)
    async_register_export_view(hass)
    return True


def _load_record_types(entry: CustomRecordsConfigEntry) -> dict[str, RecordType]:
    """Build the record-type map from the entry's record_type subentries."""
    record_types: dict[str, RecordType] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_RECORD_TYPE:
            continue
        record_type_id = subentry.unique_id
        if record_type_id is None:
            # Our own subentry flow always sets unique_id - this only guards
            # against a theoretical subentry of our type created some other
            # way, so the type checker can narrow str | None to str below.
            continue
        record_types[record_type_id] = RecordType.from_subentry(
            record_type_id, subentry.title, dict(subentry.data)
        )
    return record_types


async def _async_migrate_legacy_options(
    hass: HomeAssistant, entry: CustomRecordsConfigEntry
) -> None:
    """
    One-time migration: convert legacy options-based record types to subentries.

    Versions of this integration before record types were subentries (Phase L,
    P0-4) stored all of them as a single list in entry.options. Subentries are
    now the source of truth (each shows up as its own manageable row on the
    integration's card), so this converts any leftover options-based record
    types into subentries, then clears them from options so this is a no-op
    on every subsequent setup. Must run BEFORE the update listener is
    registered (see async_setup_entry) so these one-time updates don't
    themselves trigger a reload.
    """
    legacy = entry.options.get(CONF_RECORD_TYPES)
    if not legacy:
        return
    existing_ids = {
        subentry.unique_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_RECORD_TYPE
    }
    pending_record_types = [
        RecordType.from_dict(raw) for raw in legacy if raw["id"] not in existing_ids
    ]
    if pending_record_types:
        migration_storage = RecordStorage(hass, entry.entry_id)
        await migration_storage.async_load({})
        try:
            for record_type in pending_record_types:
                await migration_storage.async_ensure_record_type(record_type)
        finally:
            await migration_storage.async_close()
    for raw in legacy:
        record_type = RecordType.from_dict(raw)
        if record_type.id in existing_ids:
            continue
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(record_type.to_subentry_data()),
                subentry_type=SUBENTRY_TYPE_RECORD_TYPE,
                title=record_type.name,
                unique_id=record_type.id,
            ),
        )
    remaining_options = {
        key: value for key, value in entry.options.items() if key != CONF_RECORD_TYPES
    }
    hass.config_entries.async_update_entry(entry, options=remaining_options)


async def async_setup_entry(
    hass: HomeAssistant, entry: CustomRecordsConfigEntry
) -> bool:
    """Set up this integration's config entry."""
    await _async_migrate_legacy_options(hass, entry)
    record_types = _load_record_types(entry)

    storage = RecordStorage(hass, entry.entry_id)
    try:
        await storage.async_load(record_types)
    except (SchemaError, sqlite3.DatabaseError) as err:
        await storage.async_close()
        ir.async_create_issue(
            hass,
            DOMAIN,
            "database_schema_error",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="database_schema_error",
            translation_placeholders={"error": str(err)},
        )
        raise
    ir.async_delete_issue(hass, DOMAIN, "database_schema_error")

    media_store = MediaStore(hass, entry.entry_id)

    entry.runtime_data = CustomRecordsRuntimeData(
        storage=storage, media_store=media_store, record_types=record_types
    )

    await async_register_frontend(hass)

    # Startup safety net: reclaim any media files orphaned by a crash/edit
    # that happened between a purge/delete and the next scheduled cleanup.
    await media_store.async_cleanup_orphaned_media(storage, record_types)

    async def _async_purge_job(_now: object) -> None:
        del _now
        await _async_run_purge(hass, entry)

    entry.runtime_data.unsub_purge_interval = async_track_time_interval(
        hass, _async_purge_job, PURGE_INTERVAL
    )
    entry.async_on_unload(entry.runtime_data.unsub_purge_interval)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Notify any already-open card that this record type's definition may
    # have just changed (this runs on every setup, including the reload that
    # follows a config subentry add/update/remove - see _async_update_listener
    # - which is what makes this a single, sufficient place to cover ALL
    # record-type definition changes without touching config_flow.py).
    for record_type_id in record_types:
        hass.bus.async_fire(
            EVENT_RECORDS_UPDATED,
            {ATTR_ENTRY_ID: entry.entry_id, ATTR_RECORD_TYPE: record_type_id},
        )

    return True


async def async_unload_entry(
    _hass: HomeAssistant, entry: CustomRecordsConfigEntry
) -> bool:
    """
    Unload a config entry: cancel listeners, close the database connection.

    Must NOT delete any stored data - that only happens in async_remove_entry.
    """
    del _hass
    await entry.runtime_data.storage.async_close()
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: CustomRecordsConfigEntry
) -> None:
    """
    Delete all stored records when the user removes the integration.

    entry.runtime_data may already be gone by the time this runs (HA clears it
    once async_unload_entry has completed), so the storage is reconstructed
    from the entry's own persisted state rather than relying on runtime_data.
    There is one database file per entry, so removal doesn't need to load any
    record types first - it just deletes that file directly. Reads BOTH
    subentries and any not-yet-migrated legacy options only to clean up their
    Repairs issues, so removal cleans up correctly even for an entry that was
    added but never actually set up (and thus never got a chance to migrate).
    """
    record_type_ids = set(_load_record_types(entry)) | {
        rt["id"] for rt in entry.options.get(CONF_RECORD_TYPES, [])
    }
    storage = RecordStorage(hass, entry.entry_id)
    await storage.async_remove()
    await MediaStore(hass, entry.entry_id).async_remove_all()
    for record_type_id in record_type_ids:
        ir.async_delete_issue(hass, DOMAIN, f"record_count_{record_type_id}")
    ir.async_delete_issue(hass, DOMAIN, "database_schema_error")


async def _async_update_listener(
    hass: HomeAssistant, entry: CustomRecordsConfigEntry
) -> None:
    """
    Reload the entry when its record_type subentries change.

    Also purges storage/media for any record type that disappeared (subentry
    removed) compared to what's currently loaded, so removing a record type
    doesn't leave its Store file/images orphaned on disk forever. This must
    run BEFORE the reload: entry.subentries already reflects the new value at
    this point, while entry.runtime_data still holds the old (pre-reload)
    storage/media_store/record_types (HA fires update listeners before
    unloading the entry).
    """
    old_record_type_ids = set(entry.runtime_data.record_types)
    new_record_type_ids = set(_load_record_types(entry))
    for record_type_id in old_record_type_ids - new_record_type_ids:
        await entry.runtime_data.storage.async_remove_record_type(record_type_id)
        await entry.runtime_data.media_store.async_remove_record_type_media(
            record_type_id
        )
        ir.async_delete_issue(hass, DOMAIN, f"record_count_{record_type_id}")

    await hass.config_entries.async_reload(entry.entry_id)


async def _async_run_purge(
    hass: HomeAssistant, entry: CustomRecordsConfigEntry
) -> None:
    """Purge expired records, enforce max_records, and manage Repairs warnings."""
    runtime_data = entry.runtime_data
    record_types = runtime_data.record_types

    await runtime_data.storage.async_purge_expired(
        {rt_id: rt.retention_days for rt_id, rt in record_types.items()}
    )
    await runtime_data.storage.async_enforce_max_records(
        {rt_id: rt.max_records for rt_id, rt in record_types.items()}
    )
    await runtime_data.media_store.async_cleanup_orphaned_media(
        runtime_data.storage, record_types
    )

    for rt_id, record_type in record_types.items():
        issue_id = f"record_count_{rt_id}"
        if record_type.warn_at:
            count = await runtime_data.storage.async_record_count(rt_id)
            if count >= record_type.warn_at:
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="record_count_high",
                    translation_placeholders={
                        "name": record_type.name,
                        "count": str(count),
                    },
                )
            else:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    LOGGER.debug("Purge job completed for entry %s", entry.entry_id)
