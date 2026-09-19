"""Tests for custom_records.export_view.CustomRecordsExportView (authenticated)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.custom_records.const import DOMAIN

from .conftest import BP_RECORD_TYPE, async_setup_entry_with_types

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator


async def _setup_view(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})


async def test_unauthenticated_request_is_rejected(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """A request without a Bearer token or signed URL is rejected (401)."""
    await _setup_view(hass)
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    client = await hass_client_no_auth()
    resp = await client.get(f"/{DOMAIN}_export/{entry.entry_id}/bp")

    assert resp.status == 401


async def test_authenticated_request_serves_csv_full_by_default(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """An authenticated request returns full-mode CSV (id column) by default."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_add_record("bp", {"systolic": 120})

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_export/{entry.entry_id}/bp")

    assert resp.status == 200
    assert resp.content_type == "text/csv"
    assert 'attachment; filename="bp.csv"' in resp.headers["Content-Disposition"]
    text = await resp.text()
    lines = text.splitlines()
    assert lines[0] == "id,timestamp,systolic"
    assert len(lines) == 2


async def test_include_id_false_omits_id_column(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """?include_id=false drops the id column from the response."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_add_record("bp", {"systolic": 120})

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_export/{entry.entry_id}/bp?include_id=false")

    text = await resp.text()
    lines = text.splitlines()
    assert lines[0] == "timestamp,systolic"


async def test_unknown_record_type_returns_404(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A record_type_id not configured for this entry returns 404."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_export/{entry.entry_id}/unknown")

    assert resp.status == 404


async def test_unknown_entry_returns_404(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A well-formed but non-existent/unloaded entry_id returns 404."""
    await _setup_view(hass)
    assert await async_setup_component(hass, DOMAIN, {})

    client = await hass_client()
    resp = await client.get(f"/{DOMAIN}_export/does-not-exist/bp")

    assert resp.status == 404
