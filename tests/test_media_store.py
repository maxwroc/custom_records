"""Tests for custom_records.media_store."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import FormData
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.custom_records.const import (
    DOMAIN,
    FieldType,
)
from custom_components.custom_records.media_store import (
    ImageStoreError,
    MediaStore,
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


def _image_record_type() -> RecordType:
    return RecordType(
        id="pets",
        name="Pets",
        fields=[
            FieldDefinition(key="front", label="Front", type=FieldType.IMAGE),
            FieldDefinition(key="back", label="Back", type=FieldType.IMAGE),
            FieldDefinition(key="note", label="Note", type=FieldType.TEXT),
        ],
    )


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


async def test_update_record_retains_replaces_and_clears_image(
    hass: HomeAssistant, entry_id: str
) -> None:
    """An update can retain, replace, and clear a managed image."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = RecordType(
        id="pets",
        name="Pets",
        fields=[FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE)],
    )
    record_types = {"pets": record_type}
    await storage.async_load(record_types)
    original_filename = await media_store.async_store_image(
        "pets", str(make_source_image(hass, name="original.jpg"))
    )
    record = await storage.async_add_record("pets", {"photo": original_filename})
    media_source = {
        "media_source": (f"media-source://custom_records/pets/{record['id']}/photo")
    }

    retained = await media_store.async_update_record_with_images(
        storage, record_type, record["id"], {"photo": media_source}
    )
    assert retained is not None
    assert retained["d"]["photo"] == original_filename
    original_path = await media_store.async_resolve_image_path(
        "pets", original_filename
    )
    assert original_path.is_file()

    replaced = await media_store.async_update_record_with_images(
        storage,
        record_type,
        record["id"],
        {"photo": str(make_source_image(hass, name="replacement.png"))},
    )
    assert replaced is not None
    replacement_filename = replaced["d"]["photo"]
    assert replacement_filename != original_filename
    replacement_path = await media_store.async_resolve_image_path(
        "pets", replacement_filename
    )
    assert replacement_path.is_file()
    assert not original_path.exists()

    cleared = await media_store.async_update_record_with_images(
        storage, record_type, record["id"], {}
    )
    assert cleared is not None
    assert cleared["d"]["photo"] is None
    assert not replacement_path.exists()
    await storage.async_close()


async def test_update_record_rejects_forged_existing_image_reference(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Only the target record's exact public image reference can be retained."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = RecordType(
        id="pets",
        name="Pets",
        fields=[FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE)],
    )
    record_types = {"pets": record_type}
    await storage.async_load(record_types)
    filename = await media_store.async_store_image("pets", str(make_source_image(hass)))
    record = await storage.async_add_record("pets", {"photo": filename})

    with pytest.raises(ImageStoreError, match="Invalid existing image reference"):
        await media_store.async_update_record_with_images(
            storage,
            record_type,
            record["id"],
            {
                "photo": {
                    "media_source": "media-source://custom_records/pets/other/photo"
                }
            },
        )

    unchanged = await storage.async_get_record("pets", record["id"])
    assert unchanged is not None
    assert unchanged["d"]["photo"] == filename
    await storage.async_close()


async def test_failed_record_update_removes_copied_image(
    hass: HomeAssistant, entry_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newly copied image is removed when the database update fails."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = RecordType(
        id="pets",
        name="Pets",
        fields=[FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE)],
    )
    record_types = {"pets": record_type}
    await storage.async_load(record_types)
    record = await storage.async_add_record("pets", {})

    async def _fail_update(*_args: object, **_kwargs: object) -> dict[str, Any]:
        del _args, _kwargs
        msg = "disk full"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(storage, "async_update_record", _fail_update)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        await media_store.async_update_record_with_images(
            storage,
            record_type,
            record["id"],
            {"photo": str(make_source_image(hass))},
        )

    media_dir = Path(hass.config.path(".storage", DOMAIN, entry_id, "media", "pets"))
    assert await hass.async_add_executor_job(_directory_entries, media_dir) == []
    await storage.async_close()


@pytest.mark.parametrize("operation", ["add", "update"])
async def test_cancelled_record_write_preserves_committed_image(
    hass: HomeAssistant, entry_id: str, operation: str
) -> None:
    """Cancelling a running database write must not delete its staged image."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = _image_record_type()
    await storage.async_load({"pets": record_type})
    original = await storage.async_add_record("pets", {})
    fields = {"front": str(make_source_image(hass))}
    write_started = asyncio.Event()
    release_write = Event()

    def _pause_write(statement: str) -> None:
        if statement.startswith(("INSERT INTO", "UPDATE")):
            hass.loop.call_soon_threadsafe(write_started.set)
            release_write.wait()

    connection = storage._require_conn()  # noqa: SLF001
    await storage._run(connection.set_trace_callback, _pause_write)  # noqa: SLF001
    write = (
        media_store.async_add_record_with_images(storage, record_type, fields)
        if operation == "add"
        else media_store.async_update_record_with_images(
            storage, record_type, original["id"], fields
        )
    )
    task = asyncio.create_task(write)
    try:
        try:
            async with asyncio.timeout(5):
                await write_started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release_write.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        await storage._run(connection.set_trace_callback, None)  # noqa: SLF001
        references = await storage.async_list_image_references("pets")
        assert len(references) == 1
        image_path = await media_store.async_resolve_image_path(
            "pets", references[0].filenames["front"]
        )
        assert image_path.is_file()
        assert await storage.async_record_count("pets") == (
            2 if operation == "add" else 1
        )
        if operation == "update":
            assert references[0].record_id == original["id"]
        else:
            assert await storage.async_get_record("pets", original["id"]) == original
    finally:
        await storage.async_close()


async def test_unchanged_images_skip_all_cleanup_work(
    hass: HomeAssistant, entry_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text edit retaining its image does not query references or sweep files."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = _image_record_type()
    await storage.async_load({"pets": record_type})
    record = await media_store.async_add_record_with_images(
        storage, record_type, {"front": str(make_source_image(hass)), "note": "old"}
    )
    orphan = await media_store.async_store_image(
        "pets", str(make_source_image(hass, name="orphan.png"))
    )
    reference_lookup = AsyncMock(side_effect=AssertionError("Unexpected lookup"))
    full_scan = AsyncMock(side_effect=AssertionError("Unexpected full scan"))
    directory_sweep = AsyncMock(side_effect=AssertionError("Unexpected sweep"))
    monkeypatch.setattr(storage, "async_is_image_referenced", reference_lookup)
    monkeypatch.setattr(storage, "async_list_image_references", full_scan)
    monkeypatch.setattr(media_store, "_async_cleanup_orphaned_media", directory_sweep)
    try:
        updated = await media_store.async_update_record_with_images(
            storage,
            record_type,
            record["id"],
            {
                "front": {
                    "media_source": (
                        f"media-source://custom_records/pets/{record['id']}/front"
                    )
                },
                "note": "new",
            },
        )
        assert updated is not None
        assert updated["d"]["front"] == record["d"]["front"]
        assert updated["d"]["note"] == "new"
        reference_lookup.assert_not_awaited()
        full_scan.assert_not_awaited()
        directory_sweep.assert_not_awaited()
        orphan_path = await media_store.async_resolve_image_path("pets", orphan)
        assert orphan_path.is_file()
    finally:
        await storage.async_close()


@pytest.mark.parametrize("shared_field", ["front", "back"])
async def test_targeted_cleanup_preserves_images_shared_by_other_records(
    hass: HomeAssistant, entry_id: str, shared_field: str
) -> None:
    """Clearing an image never removes another record's reference in any column."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = _image_record_type()
    await storage.async_load({"pets": record_type})
    record = await media_store.async_add_record_with_images(
        storage, record_type, {"front": str(make_source_image(hass))}
    )
    filename = record["d"]["front"]
    await storage.async_add_record("pets", {shared_field: filename})
    try:
        updated = await media_store.async_update_record_with_images(
            storage, record_type, record["id"], {}
        )
        assert updated is not None
        assert updated["d"]["front"] is None
        image_path = await media_store.async_resolve_image_path("pets", filename)
        assert image_path.is_file()
    finally:
        await storage.async_close()


async def test_targeted_cleanup_preserves_image_retained_in_another_field(
    hass: HomeAssistant, entry_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filename still used by the edited row is not a cleanup candidate."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = _image_record_type()
    await storage.async_load({"pets": record_type})
    filename = await media_store.async_store_image("pets", str(make_source_image(hass)))
    record = await storage.async_add_record(
        "pets", {"front": filename, "back": filename}
    )
    reference_lookup = AsyncMock(side_effect=AssertionError("Unexpected lookup"))
    monkeypatch.setattr(storage, "async_is_image_referenced", reference_lookup)
    try:
        updated = await media_store.async_update_record_with_images(
            storage,
            record_type,
            record["id"],
            {
                "back": {
                    "media_source": (
                        f"media-source://custom_records/pets/{record['id']}/back"
                    )
                }
            },
        )
        assert updated is not None
        assert updated["d"] == {"front": None, "back": filename, "note": None}
        reference_lookup.assert_not_awaited()
        image_path = await media_store.async_resolve_image_path("pets", filename)
        assert image_path.is_file()
    finally:
        await storage.async_close()


@pytest.mark.parametrize("operation", ["add", "update"])
async def test_partial_image_copy_failure_rolls_back_only_new_files(
    hass: HomeAssistant, entry_id: str, operation: str
) -> None:
    """Both writers remove partial staging copies without touching old images."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = _image_record_type()
    await storage.async_load({"pets": record_type})
    original = await media_store.async_add_record_with_images(
        storage, record_type, {"front": str(make_source_image(hass))}
    )
    original_path = await media_store.async_resolve_image_path(
        "pets", original["d"]["front"]
    )
    fields = {
        "front": str(make_source_image(hass, name="replacement.png")),
        "back": str(_missing_path(hass)),
    }
    try:
        write = (
            media_store.async_add_record_with_images(storage, record_type, fields)
            if operation == "add"
            else media_store.async_update_record_with_images(
                storage, record_type, original["id"], fields
            )
        )
        with pytest.raises(ImageStoreError, match="not a file"):
            await write
        assert await storage.async_get_record("pets", original["id"]) == original
        assert await storage.async_record_count("pets") == 1
        assert await hass.async_add_executor_job(
            _directory_entries, original_path.parent
        ) == [original_path]
    finally:
        await storage.async_close()


async def test_cleanup_failure_does_not_roll_back_committed_image(
    hass: HomeAssistant, entry_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-commit cleanup errors propagate without deleting the new image."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = _image_record_type()
    await storage.async_load({"pets": record_type})
    original = await media_store.async_add_record_with_images(
        storage, record_type, {"front": str(make_source_image(hass))}
    )
    monkeypatch.setattr(
        storage,
        "async_is_image_referenced",
        AsyncMock(side_effect=sqlite3.OperationalError("cleanup failed")),
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="cleanup failed"):
            await media_store.async_update_record_with_images(
                storage,
                record_type,
                original["id"],
                {"front": str(make_source_image(hass, name="new.png"))},
            )
        committed = await storage.async_get_record("pets", original["id"])
        assert committed is not None
        assert committed["d"]["front"] != original["d"]["front"]
        new_path = await media_store.async_resolve_image_path(
            "pets", committed["d"]["front"]
        )
        assert new_path.is_file()
    finally:
        await storage.async_close()


async def test_missing_update_removes_staged_images(
    hass: HomeAssistant, entry_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record disappearing before persistence does not leave staged files."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = _image_record_type()
    await storage.async_load({"pets": record_type})
    original = await media_store.async_add_record_with_images(
        storage, record_type, {"front": str(make_source_image(hass))}
    )
    original_path = await media_store.async_resolve_image_path(
        "pets", original["d"]["front"]
    )
    monkeypatch.setattr(storage, "async_update_record", AsyncMock(return_value=None))
    try:
        updated = await media_store.async_update_record_with_images(
            storage,
            record_type,
            original["id"],
            {"front": str(make_source_image(hass, name="new.png"))},
        )
        assert updated is None
        assert await hass.async_add_executor_job(
            _directory_entries, original_path.parent
        ) == [original_path]
    finally:
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


async def test_add_record_with_images_replaces_path_with_reference(
    hass: HomeAssistant, entry_id: str
) -> None:
    """The production add path resolves image fields into stored filenames."""
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
    storage = RecordStorage(hass, entry_id)
    await storage.async_load({"bp": record_type})
    try:
        record = await media_store.async_add_record_with_images(
            storage, record_type, {"systolic": 120, "photo": str(source)}
        )
        assert record["d"]["systolic"] == 120
        assert record["d"]["photo"].endswith(".jpg")
        assert await storage.async_get_record("bp", record["id"]) == record
    finally:
        await storage.async_close()


async def test_add_record_without_image_fields(
    hass: HomeAssistant, entry_id: str
) -> None:
    """The shared staging path also supports record types without images."""
    media_store = MediaStore(hass, entry_id)
    record_type = RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            FieldDefinition(key="systolic", label="Systolic", type=FieldType.NUMBER)
        ],
    )

    fields = {"systolic": 120}
    storage = RecordStorage(hass, entry_id)
    await storage.async_load({"bp": record_type})
    try:
        record = await media_store.async_add_record_with_images(
            storage, record_type, fields
        )
        assert record["d"] == fields
        assert fields == {"systolic": 120}
    finally:
        await storage.async_close()


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "../secrets.yaml", "a/b.jpg", "a\\b.jpg", "x..y.jpg", "a\0.jpg"],
)
async def test_unsafe_image_filenames_rejected(
    hass: HomeAssistant, entry_id: str, filename: str
) -> None:
    """Stored values (e.g. from CSV import) never resolve or delete as paths."""
    media_store = MediaStore(hass, entry_id)
    with pytest.raises(ValueError, match="Invalid image filename"):
        await media_store.async_resolve_image_path("bp", filename)
    with pytest.raises(ValueError, match="Invalid image filename"):
        await media_store.async_delete_image("bp", filename)


async def test_cleanup_keeps_files_from_every_image_field(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Files referenced by any image field survive; rows without images are ignored."""
    media_store = MediaStore(hass, entry_id)
    storage = RecordStorage(hass, entry_id)
    record_type = RecordType(
        id="pets",
        name="Pets",
        fields=[
            FieldDefinition(key="name", label="Name", type=FieldType.TEXT),
            FieldDefinition(key="front", label="Front", type=FieldType.IMAGE),
            FieldDefinition(key="back", label="Back", type=FieldType.IMAGE),
        ],
    )
    await storage.async_load({"pets": record_type})
    front, back, orphan = [
        await media_store.async_store_image(
            "pets", str(make_source_image(hass, name=name))
        )
        for name in ("front.jpg", "back.png", "orphan.jpg")
    ]
    await storage.async_add_record("pets", {"front": front})
    await storage.async_add_record("pets", {"back": back})
    await storage.async_add_record("pets", {"name": orphan})

    removed = await media_store.async_cleanup_orphaned_media(
        storage, {"pets": record_type}
    )

    assert removed == {"pets": 1}
    for filename, kept in ((front, True), (back, True), (orphan, False)):
        path = await media_store.async_resolve_image_path("pets", filename)
        assert path.is_file() is kept


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
