"""Tests for custom_records.media_source."""

# pyright: reportArgumentType=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportOptionalIterable=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalSubscript=false

from __future__ import annotations

import pytest
from homeassistant.components.media_source import Unresolvable
from homeassistant.components.media_source.models import MediaSourceItem
from homeassistant.core import HomeAssistant

from custom_components.custom_records.const import DOMAIN
from custom_components.custom_records.media_source import CustomRecordsMediaSource

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


def _item(hass: HomeAssistant, identifier: str | None) -> MediaSourceItem:
    return MediaSourceItem(hass, DOMAIN, identifier or "", None)


async def test_browse_root_only_lists_types_with_image_fields(
    hass: HomeAssistant,
) -> None:
    """Only record types with an IMAGE field appear at the root."""
    await async_setup_entry_with_types(hass, [BP_RECORD_TYPE, IMAGE_RECORD_TYPE])
    source = CustomRecordsMediaSource(hass)

    root = await source.async_browse_media(_item(hass, None))

    child_ids = {child.identifier for child in root.children}
    assert child_ids == {"pets"}


async def test_browse_record_type_lists_records_with_images(
    hass: HomeAssistant,
) -> None:
    """Browsing a record type lists only records that have a stored image."""
    entry = await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])
    source_file = make_source_image(hass, name="cat.jpg")

    filename = await entry.runtime_data.media_store.async_store_image(
        "pets", str(source_file)
    )
    await entry.runtime_data.storage.async_add_record("pets", {"photo": filename})
    await entry.runtime_data.storage.async_add_record(
        "pets", {}
    )  # no photo, should be skipped

    source = CustomRecordsMediaSource(hass)
    browse = await source.async_browse_media(_item(hass, "pets"))

    assert len(browse.children) == 1
    assert browse.children[0].identifier.startswith("pets/")
    assert browse.children[0].can_play is True


async def test_resolve_media_returns_playmedia(hass: HomeAssistant) -> None:
    """Resolving a valid identifier returns a PlayMedia pointing at the stored file."""
    entry = await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])
    source_file = make_source_image(hass, name="cat.jpg")

    filename = await entry.runtime_data.media_store.async_store_image(
        "pets", str(source_file)
    )
    record = await entry.runtime_data.storage.async_add_record(
        "pets", {"photo": filename}
    )

    source = CustomRecordsMediaSource(hass)
    play_media = await source.async_resolve_media(
        _item(hass, f"pets/{record['id']}/photo")
    )

    assert play_media.path.is_file()
    assert play_media.url == (f"/custom_records_media/{entry.entry_id}/pets/{filename}")


async def test_resolve_media_unknown_record_raises(hass: HomeAssistant) -> None:
    """Resolving an unknown record id raises Unresolvable."""
    await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])
    source = CustomRecordsMediaSource(hass)

    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item(hass, "pets/missing-id/photo"))


async def test_resolve_media_invalid_identifier_raises(hass: HomeAssistant) -> None:
    """A malformed identifier raises Unresolvable rather than crashing."""
    await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])
    source = CustomRecordsMediaSource(hass)

    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item(hass, "not-enough-parts"))
