"""
CSV export download endpoint for a single record type.

Mirrors media_store.py's CustomRecordsMediaView pattern: a real
HomeAssistantView (requires_auth=True by default), reachable either with a
Bearer token or a short-lived signed-URL query param (see config_flow.py's
`export_data` step, which builds and signs the actual download link shown to
the user).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.http import HomeAssistantView

from .const import ATTR_INCLUDE_ID, DOMAIN, EXPORT_URL_PREFIX
from .csv_transfer import build_export_csv

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .runtime_data import CustomRecordsRuntimeData

_EXPORT_VIEW_REGISTERED_KEY = "custom_records_export_view_registered"


def _get_runtime_data(
    hass: HomeAssistant, entry_id: str
) -> CustomRecordsRuntimeData | None:
    """Return the runtime data for entry_id, if it's currently loaded."""
    entries = hass.config_entries.async_entries(DOMAIN)
    matches = [
        entry
        for entry in entries
        if entry.entry_id == entry_id and entry.state is ConfigEntryState.LOADED
    ]
    return matches[0].runtime_data if matches else None


class CustomRecordsExportView(HomeAssistantView):
    """Serve a record type's records as a downloadable CSV file."""

    url = f"{EXPORT_URL_PREFIX}/{{entry_id}}/{{record_type_id}}"
    name = "api:custom_records:export"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, record_type_id: str
    ) -> web.Response:
        """Handle a GET request: build and return the CSV for one record type."""
        runtime_data = _get_runtime_data(self.hass, entry_id)
        if runtime_data is None:
            raise web.HTTPNotFound
        record_type = runtime_data.record_types.get(record_type_id)
        if record_type is None:
            raise web.HTTPNotFound

        include_id = request.query.get(ATTR_INCLUDE_ID, "true").lower() != "false"
        records = await runtime_data.storage.async_list_records(record_type_id)
        csv_text = build_export_csv(record_type, records, include_id=include_id)

        return web.Response(
            text=csv_text,
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{record_type_id}.csv"'
            },
        )


def async_register_export_view(hass: HomeAssistant) -> None:
    """Register the authenticated export-download view, once, hass-wide."""
    if hass.data.get(_EXPORT_VIEW_REGISTERED_KEY):
        return
    hass.http.register_view(CustomRecordsExportView(hass))
    hass.data[_EXPORT_VIEW_REGISTERED_KEY] = True
