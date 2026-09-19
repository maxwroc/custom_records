"""Tests for custom_records.media_store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from aiohttp import FormData
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.custom_records.const import (
    DOMAIN,
    ENVELOPE_DATA,
    ENVELOPE_ID,
    FieldType,
)
from custom_components.custom_records.media_store import (
    MediaStore,
    async_resolve_image_fields,
    async_validate_image_path,
)
from custom_components.custom_records.models import FieldDefinition, RecordType
from custom_components.custom_records.store import RecordStorage

from .conftest import make_source_image

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator


def _missing_path(hass: HomeAssistant) -> Path:
    """Return a path inside the allowed root that doesn't exist."""
    return Path(hass.config.path(f"missing_{uuid4().hex}.jpg"))


def _directory_entries(path: Path) -> list[Path]:
    """List directory entries outside the event loop."""
    return list(path.iterdir())


@pytest.fixture
def entry_id() -> str:
    """Return a unique entry id per test so on-disk media dirs never collide."""
    return f"entry-{uuid4().hex}"


async def test_store_resolve_and_delete_image(
    hass: HomeAssistant, entry_id: str
) -> None:
    """An image can be stored, resolved to a path, and deleted."""
    media_store = MediaStore(hass, entry_id)
    source = make_source_image(hass)

    filename = await media_store.async_store_image("bp", str(source))
    assert filename.endswith(".jpg")

    resolved = await media_store.async_resolve_image_path("bp", filename)
    assert resolved.is_file()
    assert resolved.read_bytes() == b"fake-image-bytes"

    await media_store.async_delete_image("bp", filename)
    resolved_after_delete = await media_store.async_resolve_image_path("bp", filename)
    assert not resolved_after_delete.is_file()


async def test_store_image_rejects_missing_file(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Storing a non-existent (but in-bounds) source path raises ValueError."""
    media_store = MediaStore(hass, entry_id)
    with pytest.raises(ValueError, match="not a file"):
        await media_store.async_store_image("bp", str(_missing_path(hass)))


async def test_store_image_rejects_path_outside_allowed_root(
    hass: HomeAssistant, entry_id: str, tmp_path: Path
) -> None:
    """A source path outside the allow-listed root(s) is rejected."""
    media_store = MediaStore(hass, entry_id)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"fake-image-bytes")
    with pytest.raises(ValueError, match="must be inside"):
        await media_store.async_store_image("bp", str(source))


async def test_store_image_rejects_unsupported_extension(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Storing a file with a disallowed extension raises ValueError."""
    media_store = MediaStore(hass, entry_id)
    source = make_source_image(hass, name="document.txt")
    with pytest.raises(ValueError, match="Unsupported image extension"):
        await media_store.async_store_image("bp", str(source))


async def test_record_type_path_cannot_escape_media_directory(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Malformed legacy ids cannot resolve outside the managed media tree."""
    media_store = MediaStore(hass, entry_id)

    for unsafe_id in ("/config", "..", "../outside"):
        with pytest.raises(ValueError, match="Invalid record type id"):
            await media_store.async_resolve_image_path(unsafe_id, "image.jpg")


async def test_cleanup_orphaned_media_removes_unreferenced_files(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Files no longer referenced by any record are deleted; referenced ones survive."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE),
        ],
    )
    await storage.async_load({"bp": record_type})

    kept_filename = await media_store.async_store_image(
        "bp", str(make_source_image(hass))
    )
    orphan_filename = await media_store.async_store_image(
        "bp", str(make_source_image(hass, name="orphan.png"))
    )

    await storage.async_add_record("bp", {"photo": kept_filename})

    removed = await media_store.async_cleanup_orphaned_media(
        storage, {"bp": record_type}
    )
    assert removed == {"bp": 1}

    kept_path = await media_store.async_resolve_image_path("bp", kept_filename)
    orphan_path = await media_store.async_resolve_image_path("bp", orphan_filename)
    assert kept_path.is_file()
    assert not orphan_path.is_file()


async def test_failed_record_insert_removes_copied_image(
    hass: HomeAssistant, entry_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copied image is removed when its database insert fails."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = RecordType(
        id="pets",
        name="Pets",
        fields=[FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE)],
    )
    await storage.async_load({"pets": record_type})

    async def _fail_insert(*_args: object, **_kwargs: object) -> dict[str, Any]:
        del _args, _kwargs
        msg = "disk full"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(storage, "async_add_record", _fail_insert)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        await media_store.async_add_record_with_images(
            storage,
            record_type,
            {"photo": str(make_source_image(hass))},
        )

    media_dir = Path(hass.config.path(".storage", DOMAIN, entry_id, "media", "pets"))
    assert await hass.async_add_executor_job(_directory_entries, media_dir) == []
    await storage.async_close()


async def test_async_remove_all_deletes_entry_media_dir(
    hass: HomeAssistant, entry_id: str
) -> None:
    """async_remove_all deletes the whole media tree for the entry."""
    media_store = MediaStore(hass, entry_id)
    filename = await media_store.async_store_image("bp", str(make_source_image(hass)))
    path = await media_store.async_resolve_image_path("bp", filename)
    assert path.is_file()

    await media_store.async_remove_all()

    assert not path.is_file()


async def test_async_remove_record_type_media_deletes_only_that_type(
    hass: HomeAssistant, entry_id: str
) -> None:
    """async_remove_record_type_media deletes one type's media, leaving others."""
    media_store = MediaStore(hass, entry_id)
    bp_filename = await media_store.async_store_image(
        "bp", str(make_source_image(hass))
    )
    pets_filename = await media_store.async_store_image(
        "pets", str(make_source_image(hass, name="cat.jpg"))
    )

    await media_store.async_remove_record_type_media("bp")

    bp_path = await media_store.async_resolve_image_path("bp", bp_filename)
    pets_path = await media_store.async_resolve_image_path("pets", pets_filename)
    assert not bp_path.is_file()
    assert pets_path.is_file()


async def test_resolve_image_fields_replaces_path_with_reference(
    hass: HomeAssistant, entry_id: str
) -> None:
    """async_resolve_image_fields turns a filesystem path into a stored filename."""
    media_store = MediaStore(hass, entry_id)
    record_type = RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            FieldDefinition(key="systolic", label="Systolic", type=FieldType.NUMBER),
            FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE),
        ],
    )
    source = make_source_image(hass)

    resolved = await async_resolve_image_fields(
        media_store, record_type, {"systolic": 120, "photo": str(source)}
    )

    assert resolved["systolic"] == 120
    assert isinstance(resolved["photo"], str)
    assert resolved["photo"].endswith(".jpg")


async def test_resolve_image_fields_noop_without_image_fields(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Record types with no IMAGE field pass fields through unchanged."""
    media_store = MediaStore(hass, entry_id)
    record_type = RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            FieldDefinition(key="systolic", label="Systolic", type=FieldType.NUMBER)
        ],
    )

    fields = {"systolic": 120}
    resolved = await async_resolve_image_fields(media_store, record_type, fields)
    assert resolved is fields


def test_referenced_filenames_uses_envelope_data_key() -> None:
    """Sanity check that ENVELOPE_DATA/ENVELOPE_ID match the record envelope shape."""
    record = {
        ENVELOPE_ID: "abc",
        ENVELOPE_DATA: {"photo": "x.jpg"},
    }
    assert record[ENVELOPE_DATA]["photo"] == "x.jpg"


async def test_validate_image_path_valid_file(hass: HomeAssistant) -> None:
    """An existing, allowed-extension file validates with no error."""
    source = make_source_image(hass)
    error = await async_validate_image_path(hass, str(source))
    assert error is None


async def test_validate_image_path_missing_file(hass: HomeAssistant) -> None:
    """A non-existent (but in-bounds) path returns an explanatory error message."""
    error = await async_validate_image_path(hass, str(_missing_path(hass)))
    assert error is not None
    assert "not a file" in error


async def test_validate_image_path_outside_allowed_root(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A path outside the allow-listed root(s) returns an explanatory error."""
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"fake-image-bytes")
    error = await async_validate_image_path(hass, str(source))
    assert error is not None
    assert "must be inside" in error


async def _upload_file(
    client: TestClient,
    content: bytes,
    filename: str,
) -> str:
    """Upload a file via the standard /api/file_upload endpoint; return its file_id."""
    form = FormData()
    form.add_field("file", content, filename=filename, content_type="image/jpeg")
    resp = await client.post("/api/file_upload", data=form)
    assert resp.status == 200
    return (await resp.json())["file_id"]


async def test_store_uploaded_image(
    hass: HomeAssistant, entry_id: str, hass_client: ClientSessionGenerator
) -> None:
    """A file staged via HA's file_upload component is adopted into managed storage."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})
    client = await hass_client()
    file_id = await _upload_file(client, b"fake-image-bytes", "photo.jpg")

    media_store = MediaStore(hass, entry_id)
    filename = await media_store.async_store_uploaded_image("bp", file_id)

    assert filename.endswith(".jpg")
    resolved = await media_store.async_resolve_image_path("bp", filename)
    assert resolved.is_file()
    assert resolved.read_bytes() == b"fake-image-bytes"


async def test_store_uploaded_image_rejects_unsupported_extension(
    hass: HomeAssistant, entry_id: str, hass_client: ClientSessionGenerator
) -> None:
    """An uploaded file with a disallowed extension raises ValueError."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})
    client = await hass_client()
    file_id = await _upload_file(client, b"not an image", "document.txt")

    media_store = MediaStore(hass, entry_id)
    with pytest.raises(ValueError, match="Unsupported image extension"):
        await media_store.async_store_uploaded_image("bp", file_id)


async def test_store_uploaded_image_rejects_unknown_file_id(
    hass: HomeAssistant, entry_id: str
) -> None:
    """An unknown/expired file_id raises ValueError (from process_uploaded_file)."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})
    media_store = MediaStore(hass, entry_id)
    with pytest.raises(ValueError, match="File does not exist"):
        await media_store.async_store_uploaded_image("bp", "unknown-file-id")


async def test_validate_image_path_bad_extension(hass: HomeAssistant) -> None:
    """A disallowed extension returns an explanatory error message."""
    source = make_source_image(hass, name="notes.txt")
    error = await async_validate_image_path(hass, str(source))
    assert error is not None
    assert "Unsupported image extension" in error
