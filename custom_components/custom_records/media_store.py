"""
Image file storage for IMAGE-type record fields.

Files are stored under
<config>/.storage/custom_records/<entry_id>/media/<record_type_id>/, decoupled
from the record's own id (a random filename is generated per stored image) so
an image can be stored before its owning record exists.

The service caller is treated as trusted: no deep content/decompression-bomb
validation is done, per the project's agreed scope. The source path is still
required to resolve to a real file, with an allowed image extension, inside
an allow-listed root directory (the HA config dir, i.e. `/config` - see
allowed_source_roots) so that neither the add_record service/WS command nor
the validate_image_path WS command can be used to probe or copy arbitrary
files from elsewhere on the host filesystem.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from aiohttp import web
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.helpers.http import HomeAssistantView

from .const import DOMAIN, FieldType

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from .models import RecordType
    from .store import RecordStorage

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# URL prefix under which each entry's media directory is served, via
# CustomRecordsMediaView below - a real HomeAssistantView (requires_auth=True
# by default), the same authenticated-view mechanism HA's own local media
# source uses for config/media (see LocalMediaView). A request must carry
# either a Bearer token or a valid signed-URL query param (the same signing
# HA applies automatically when media is resolved via the core
# `media_source/resolve_media` WebSocket command) to succeed.
MEDIA_URL_PREFIX = f"/{DOMAIN}_media"
_MEDIA_VIEW_REGISTERED_KEY = f"{DOMAIN}_media_view_registered"


class ImageStoreError(ValueError):
    """Raised when an image cannot be validated or copied into managed storage."""


def _remove_unreferenced_files(target_dir: Path, referenced: set[str]) -> int:
    """Delete files in target_dir whose name isn't in referenced. Runs in executor."""
    if not target_dir.is_dir():
        return 0
    removed = 0
    for path in target_dir.iterdir():
        if path.is_file() and path.name not in referenced:
            path.unlink()
            removed += 1
    return removed


def allowed_source_roots(hass: HomeAssistant) -> list[Path]:
    """
    Return the directories file/path fields are allowed to resolve into.

    Always includes the HA config directory (e.g. /config). Also includes
    /workspaces if it exists on disk - a dev-container-only convenience (this
    repo and its test config live there); a real HA install never has this
    directory, so production installs only ever get /config. Used both for
    IMAGE field source paths and for CSV export/import `path` service params
    (csv_transfer.py / services.py) - the same allow-listed-root protection
    applies to any user-supplied filesystem path.
    """
    roots = [Path(hass.config.path()).resolve()]
    workspaces = Path("/workspaces")
    if workspaces.is_dir():
        roots.append(workspaces.resolve())
    return roots


def validate_source_path(
    source_path: str,
    allowed_roots: list[Path],
    allowed_extensions: set[str],
    kind: str = "Image",
) -> Path:
    """
    Validate a path to an EXISTING file is within an allowed root/extension.

    Runs in the executor (does blocking filesystem I/O). Raises ValueError
    with a user-facing message if invalid. The allow-list containment check
    happens BEFORE any filesystem existence check, so a path outside the
    allow-list is rejected without ever touching disk - this can't be used as
    an oracle to probe for the existence of arbitrary files elsewhere on the
    host filesystem. `kind` customizes the error messages (e.g. "Image"/"CSV")
    for the caller's context; callers outside media_store.py (e.g. CSV import)
    pass their own `allowed_extensions`/`kind`.
    """
    source = Path(source_path).resolve()
    if not any(source.is_relative_to(root) for root in allowed_roots):
        msg = f"{kind} source path must be inside {allowed_roots[0]}"
        raise ValueError(msg)
    if not source.is_file():
        msg = f"{kind} source path is not a file: {source}"
        raise ValueError(msg)
    ext = source.suffix.lower()
    if ext not in allowed_extensions:
        msg = f"Unsupported {kind.lower()} extension '{ext}'"
        raise ValueError(msg)
    return source


def validate_write_target_path(
    target_path: str,
    allowed_roots: list[Path],
    allowed_extensions: set[str],
    kind: str = "Export",
) -> Path:
    """
    Validate a path safe to WRITE a new file to (e.g. export_records' `path`).

    Same allow-listed-root/extension protection as validate_source_path, but
    does NOT require the target file to already exist - only that its parent
    directory does (the file itself is about to be created).
    """
    target = Path(target_path).resolve()
    if not any(target.is_relative_to(root) for root in allowed_roots):
        msg = f"{kind} target path must be inside {allowed_roots[0]}"
        raise ValueError(msg)
    ext = target.suffix.lower()
    if ext not in allowed_extensions:
        msg = f"Unsupported {kind.lower()} extension '{ext}'"
        raise ValueError(msg)
    if not target.parent.is_dir():
        msg = f"{kind} target directory does not exist: {target.parent}"
        raise ValueError(msg)
    return target


class CustomRecordsMediaView(HomeAssistantView):
    """Serve stored images - authenticated (Bearer token or signed URL)."""

    url = f"{MEDIA_URL_PREFIX}/{{entry_id}}/{{record_type_id}}/{{filename}}"
    name = f"api:{DOMAIN}:media"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        record_type_id: str,
        filename: str,
    ) -> web.FileResponse:
        """Handle a GET request for a single stored image file."""
        del request
        try:
            path = await MediaStore(self.hass, entry_id).async_resolve_image_path(
                record_type_id, filename
            )
        except ValueError as err:
            raise web.HTTPBadRequest from err
        if not await self.hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        return web.FileResponse(path)


def async_register_media_view(hass: HomeAssistant) -> None:
    """Register the authenticated media-serving view, once, hass-wide."""
    if hass.data.get(_MEDIA_VIEW_REGISTERED_KEY):
        return
    hass.http.register_view(CustomRecordsMediaView(hass))
    hass.data[_MEDIA_VIEW_REGISTERED_KEY] = True


class MediaStore:
    """Manages image files for IMAGE-type record fields, one dir per entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the media store for a config entry."""
        self.hass = hass
        self.entry_id = entry_id
        self._base_dir = Path(hass.config.path(".storage", DOMAIN, entry_id, "media"))
        self._operation_lock = asyncio.Lock()

    def _dir_for_type(self, record_type_id: str) -> Path:
        target = (self._base_dir / record_type_id).resolve()
        base_dir = self._base_dir.resolve()
        if not target.is_relative_to(base_dir) or target == base_dir:
            msg = f"Invalid record type id '{record_type_id}'"
            raise ValueError(msg)
        return target

    def _image_path(self, record_type_id: str, filename: str) -> Path:
        """
        Return a stored image's path, rejecting anything but a plain filename.

        Stored values can come from CSV imports, so never trust them as paths.
        """
        if (
            not filename
            or filename == "."
            or ".." in filename
            or any(sep in filename for sep in ("/", "\\", "\0"))
        ):
            msg = f"Invalid image filename '{filename}'"
            raise ValueError(msg)
        return self._dir_for_type(record_type_id) / filename

    async def async_store_image(self, record_type_id: str, source_path: str) -> str:
        """Copy source_path into the managed media dir; return the stored filename."""

        def _copy() -> str:
            source = validate_source_path(
                source_path, allowed_source_roots(self.hass), ALLOWED_IMAGE_EXTENSIONS
            )
            target_dir = self._dir_for_type(record_type_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid4()}{source.suffix.lower()}"
            shutil.copyfile(source, target_dir / filename)
            return filename

        return await self.hass.async_add_executor_job(_copy)

    async def async_store_uploaded_image(
        self, record_type_id: str, file_id: str
    ) -> str:
        """
        Adopt a file uploaded via HA's file_upload component.

        Returns the stored filename. Raises ValueError (unknown/expired
        file_id, from process_uploaded_file itself, or an unsupported
        extension) - handled identically to async_store_image by callers
        (see async_add_record_with_images).
        """

        def _copy() -> str:
            with process_uploaded_file(self.hass, file_id) as uploaded_path:
                ext = uploaded_path.suffix.lower()
                if ext not in ALLOWED_IMAGE_EXTENSIONS:
                    msg = f"Unsupported image extension '{ext}'"
                    raise ValueError(msg)
                target_dir = self._dir_for_type(record_type_id)
                target_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{uuid4()}{ext}"
                shutil.copyfile(uploaded_path, target_dir / filename)
                return filename

        return await self.hass.async_add_executor_job(_copy)

    async def async_resolve_image_path(
        self, record_type_id: str, filename: str
    ) -> Path:
        """Return the absolute path to a stored image file (ValueError if unsafe)."""
        return self._image_path(record_type_id, filename)

    async def async_delete_image(self, record_type_id: str, filename: str) -> None:
        """Delete a single stored image file, ignoring if already missing."""
        path = self._image_path(record_type_id, filename)

        def _delete() -> None:
            path.unlink(missing_ok=True)

        await self.hass.async_add_executor_job(_delete)

    async def async_cleanup_orphaned_media(
        self,
        record_storage: RecordStorage,
        record_types: dict[str, RecordType],
    ) -> dict[str, int]:
        """Delete stored image files no longer referenced by any record."""
        async with self._operation_lock:
            return await self._async_cleanup_orphaned_media(
                record_storage, record_types
            )

    async def _async_cleanup_orphaned_media(
        self,
        record_storage: RecordStorage,
        record_types: dict[str, RecordType],
    ) -> dict[str, int]:
        """Delete orphaned media while the caller holds the media operation lock."""
        removed_counts: dict[str, int] = {}
        for record_type_id, record_type in record_types.items():
            if not any(f.type is FieldType.IMAGE for f in record_type.fields):
                continue

            references = await record_storage.async_list_image_references(
                record_type_id
            )
            referenced = {
                filename
                for _, filenames in references
                for filename in filenames.values()
            }
            target_dir = self._dir_for_type(record_type_id)
            removed_counts[record_type_id] = await self.hass.async_add_executor_job(
                _remove_unreferenced_files, target_dir, referenced
            )
        return removed_counts

    async def async_add_record_with_images(
        self,
        record_storage: RecordStorage,
        record_type: RecordType,
        fields: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Copy image fields and insert their record as one media operation."""
        async with self._operation_lock:
            resolved = dict(fields)
            copied_filenames: list[str] = []
            try:
                for field_def in record_type.fields:
                    if field_def.type is not FieldType.IMAGE:
                        continue
                    value = resolved.get(field_def.key)
                    if not value:
                        continue
                    try:
                        if isinstance(value, dict):
                            filename = await self.async_store_uploaded_image(
                                record_type.id, value["file_id"]
                            )
                        else:
                            filename = await self.async_store_image(
                                record_type.id, value
                            )
                    except ValueError as err:
                        raise ImageStoreError(str(err)) from err
                    copied_filenames.append(filename)
                    resolved[field_def.key] = filename
                return await record_storage.async_add_record(
                    record_type.id, resolved, timestamp
                )
            except BaseException:
                for filename in copied_filenames:
                    await self.async_delete_image(record_type.id, filename)
                raise

    async def async_remove_all(self) -> None:
        """Delete the entire media directory tree for this entry (uninstall)."""

        def _remove() -> None:
            shutil.rmtree(self._base_dir, ignore_errors=True)

        await self.hass.async_add_executor_job(_remove)

    async def async_remove_record_type_media(self, record_type_id: str) -> None:
        """Delete all stored images for a single record type (e.g. type removed)."""

        def _remove() -> None:
            shutil.rmtree(self._dir_for_type(record_type_id), ignore_errors=True)

        await self.hass.async_add_executor_job(_remove)


async def async_resolve_image_fields(
    media_store: MediaStore,
    record_type: RecordType,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """
    Store any IMAGE-type field values (filesystem paths) and replace them.

    Shared between services.py and websocket_api.py so both write paths apply
    the same handling.
    """
    image_field_keys = {f.key for f in record_type.fields if f.type is FieldType.IMAGE}
    if not image_field_keys:
        return fields

    resolved = dict(fields)
    for key in image_field_keys:
        source_path = resolved.get(key)
        if not source_path:
            continue
        filename = await media_store.async_store_image(record_type.id, source_path)
        resolved[key] = filename
    return resolved


async def async_validate_image_path(
    hass: HomeAssistant, source_path: str
) -> str | None:
    """
    Return None if source_path is a valid, existing image file, else an error message.

    Used by the custom_records/validate_image_path WebSocket command so the
    card can check a path before submitting an add_record call.
    """

    def _check() -> str | None:
        try:
            validate_source_path(
                source_path, allowed_source_roots(hass), ALLOWED_IMAGE_EXTENSIONS
            )
        except ValueError as err:
            return str(err)
        return None

    return await hass.async_add_executor_job(_check)
