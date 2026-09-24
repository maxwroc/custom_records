"""
Media source: browse/resolve images stored for IMAGE-type record fields.

Exposes a simple two-level hierarchy: root -> record types that have an image
field -> individual records with a stored image as leaf items.

Images are served through CustomRecordsMediaView (media_store.py), a real
HomeAssistantView with requires_auth=True (the same authenticated-view
mechanism HA's own local media source uses for config/media). The URL
returned below is unsigned; callers should resolve media via the core
`media_source/resolve_media` WebSocket command (not by constructing this URL
directly), since that's what wraps it with a short-lived signed-URL token via
homeassistant.components.http.auth.async_sign_path - required for e.g. <img>
tags, which can't send an Authorization header.
"""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING

from homeassistant.components.media_player.const import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntryState

from .const import DOMAIN, ENVELOPE_DATA, ENVELOPE_ID, FieldType, RecordOrder
from .media_store import MEDIA_URL_PREFIX

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .runtime_data import CustomRecordsRuntimeData


async def async_get_media_source(hass: HomeAssistant) -> CustomRecordsMediaSource:
    """Set up the custom_records media source."""
    return CustomRecordsMediaSource(hass)


def _get_runtime_data(hass: HomeAssistant) -> CustomRecordsRuntimeData | None:
    entries = hass.config_entries.async_entries(DOMAIN)
    loaded = [entry for entry in entries if entry.state is ConfigEntryState.LOADED]
    return loaded[0].runtime_data if loaded else None


class CustomRecordsMediaSource(MediaSource):
    """Expose stored record images through HA's media browser."""

    name = "Custom Records"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a record's image field to a playable/servable URL."""
        runtime_data = _get_runtime_data(self.hass)
        if runtime_data is None:
            msg = "Custom Records is not set up"
            raise Unresolvable(msg)

        try:
            record_type_id, record_id, field_key = item.identifier.split("/", 2)
        except ValueError as err:
            msg = "Invalid media identifier"
            raise Unresolvable(msg) from err

        page = await runtime_data.storage.async_list_records(
            record_type_id, order=RecordOrder.ASC
        )
        record = next(
            (r for r in page.records if r[ENVELOPE_ID] == record_id),
            None,
        )
        if record is None:
            msg = f"Record '{record_id}' not found"
            raise Unresolvable(msg)

        filename = record[ENVELOPE_DATA].get(field_key)
        if not isinstance(filename, str) or not filename:
            msg = f"No image stored for field '{field_key}'"
            raise Unresolvable(msg)

        path = await runtime_data.media_store.async_resolve_image_path(
            record_type_id, filename
        )
        if not await self.hass.async_add_executor_job(path.is_file):
            msg = "Image file is missing on disk"
            raise Unresolvable(msg)

        mime_type, _ = mimetypes.guess_type(str(path))
        entry_id = runtime_data.storage.entry_id
        url = f"{MEDIA_URL_PREFIX}/{entry_id}/{record_type_id}/{filename}"
        return PlayMedia(url, mime_type or "application/octet-stream", path=path)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse record types with image fields, then records with images."""
        runtime_data = _get_runtime_data(self.hass)
        if runtime_data is None:
            msg = "Custom Records is not set up"
            raise Unresolvable(msg)

        if not item.identifier:
            return self._browse_root(runtime_data)
        return await self._browse_record_type(runtime_data, item.identifier)

    def _browse_root(self, runtime_data: CustomRecordsRuntimeData) -> BrowseMediaSource:
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=record_type.id,
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.IMAGE,
                title=record_type.name,
                can_play=False,
                can_expand=True,
            )
            for record_type in runtime_data.record_types.values()
            if any(f.type is FieldType.IMAGE for f in record_type.fields)
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.IMAGE,
            title="Custom Records",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _browse_record_type(
        self, runtime_data: CustomRecordsRuntimeData, record_type_id: str
    ) -> BrowseMediaSource:
        record_type = runtime_data.record_types.get(record_type_id)
        if record_type is None:
            msg = f"Unknown record_type '{record_type_id}'"
            raise Unresolvable(msg)

        image_field_keys = [
            f.key for f in record_type.fields if f.type is FieldType.IMAGE
        ]
        children = []
        page = await runtime_data.storage.async_list_records(
            record_type_id, order=RecordOrder.ASC
        )
        for record in page.records:
            for field_key in image_field_keys:
                value = record[ENVELOPE_DATA].get(field_key)
                if not isinstance(value, str) or not value:
                    continue
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{record_type_id}/{record[ENVELOPE_ID]}/{field_key}",
                        media_class=MediaClass.IMAGE,
                        media_content_type=MediaType.IMAGE,
                        title=record.get("t", record[ENVELOPE_ID]),
                        can_play=True,
                        can_expand=False,
                    )
                )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=record_type_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.IMAGE,
            title=record_type.name,
            can_play=False,
            can_expand=True,
            children=children,
        )
