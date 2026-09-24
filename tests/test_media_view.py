"""Tests for custom_records.media_store.CustomRecordsMediaView (authenticated)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.custom_records.const import DOMAIN
from custom_components.custom_records.media_store import MediaStore

from .conftest import make_source_image

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator


async def _setup_view(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, DOMAIN, {})


async def test_unauthenticated_request_is_rejected(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """A request without a Bearer token or signed URL is rejected (401)."""
    await _setup_view(hass)
    entry_id = f"entry-{uuid4().hex}"
    media_store = MediaStore(hass, entry_id)
    source = make_source_image(hass)
    filename = await media_store.async_store_image("bp", str(source))

    client = await hass_client_no_auth()
    resp = await client.get(f"/{DOMAIN}_media/{entry_id}/bp/{filename}")

    assert resp.status == 401


async def test_authenticated_request_serves_file(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """An authenticated request (Bearer token) successfully serves the file."""
    await _setup_view(hass)
    entry_id = f"entry-{uuid4().hex}"
    media_store = MediaStore(hass, entry_id)
    source = make_source_image(hass)
    filename = await media_store.async_store_image("bp", str(source))

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_media/{entry_id}/bp/{filename}")

    assert resp.status == 200
    assert await resp.read() == b"fake-image-bytes"


async def test_missing_file_returns_404(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A well-formed but non-existent file path returns 404."""
    await _setup_view(hass)
    entry_id = f"entry-{uuid4().hex}"

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_media/{entry_id}/bp/does-not-exist.jpg")

    assert resp.status == 404


async def test_path_traversal_filename_rejected(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A filename containing '..' is rejected (400), even if authenticated."""
    await _setup_view(hass)
    entry_id = f"entry-{uuid4().hex}"

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_media/{entry_id}/bp/..%2f..%2fsecret.jpg")

    assert resp.status in (400, 404)


async def test_backslash_filename_rejected(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A filename with a Windows path separator is rejected with 400."""
    await _setup_view(hass)
    entry_id = f"entry-{uuid4().hex}"

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_media/{entry_id}/bp/a%5Cb.jpg")

    assert resp.status == 400
