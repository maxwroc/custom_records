"""Tests for custom_records.media_source."""

# pyright: reportArgumentType=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportOptionalIterable=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalSubscript=false

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components.media_source import Unresolvable
from homeassistant.components.media_source.models import MediaSourceItem
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

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


async def test_browse_lists_each_image_field_oldest_first(
    hass: HomeAssistant,
) -> None:
    """Every stored image field becomes a leaf titled by its record id."""
    record_type = {
        **IMAGE_RECORD_TYPE,
        "fields": [
            *IMAGE_RECORD_TYPE["fields"],
            {**IMAGE_RECORD_TYPE["fields"][0], "key": "scan", "label": "Scan"},
        ],
    }
    entry = await async_setup_entry_with_types(hass, [record_type])
    storage = entry.runtime_data.storage
    now = dt_util.utcnow()
    newer = await storage.async_add_record("pets", {"scan": "s.png"}, timestamp=now)
    older = await storage.async_add_record(
        "pets",
        {"photo": "p.jpg", "scan": "o.png"},
        timestamp=now - timedelta(days=1),
    )

    browse = await CustomRecordsMediaSource(hass).async_browse_media(
        _item(hass, "pets")
    )

    assert [(c.identifier, c.title) for c in browse.children] == [
        (f"pets/{older['id']}/photo", older["id"]),
        (f"pets/{older['id']}/scan", older["id"]),
        (f"pets/{newer['id']}/scan", newer["id"]),
    ]


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


@pytest.mark.parametrize(
    "identifier_template",
    ["pets/{id}/name", "pets/{id}/missing", "unknown/{id}/photo"],
)
async def test_resolve_media_requires_image_field(
    hass: HomeAssistant, identifier_template: str
) -> None:
    """Only configured image fields resolve; other field values are never paths."""
    record_type = {
        **IMAGE_RECORD_TYPE,
        "fields": [
            *IMAGE_RECORD_TYPE["fields"],
            {**IMAGE_RECORD_TYPE["fields"][0], "key": "name", "type": "text"},
        ],
    }
    entry = await async_setup_entry_with_types(hass, [record_type])
    record = await entry.runtime_data.storage.async_add_record(
        "pets", {"name": "cat.jpg"}
    )
    source = CustomRecordsMediaSource(hass)

    with pytest.raises(Unresolvable):
        await source.async_resolve_media(
            _item(hass, identifier_template.format(id=record["id"]))
        )


async def test_resolve_media_rejects_unsafe_stored_filename(
    hass: HomeAssistant,
) -> None:
    """An imported traversal value in an image field is not resolved to a path."""
    entry = await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])
    record = await entry.runtime_data.storage.async_add_record(
        "pets", {"photo": "../../secrets.yaml"}
    )

    with pytest.raises(Unresolvable, match="Invalid stored image"):
        await CustomRecordsMediaSource(hass).async_resolve_media(
            _item(hass, f"pets/{record['id']}/photo")
        )


async def test_resolve_media_invalid_identifier_raises(hass: HomeAssistant) -> None:
    """A malformed identifier raises Unresolvable rather than crashing."""
    await async_setup_entry_with_types(hass, [IMAGE_RECORD_TYPE])
    source = CustomRecordsMediaSource(hass)

    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item(hass, "not-enough-parts"))
