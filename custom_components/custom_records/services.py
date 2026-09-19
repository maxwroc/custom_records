"""The custom_records services: add_record, export_records, import_records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CONTENT,
    ATTR_FIELDS,
    ATTR_INCLUDE_ID,
    ATTR_PATH,
    ATTR_RECORD_TYPE,
    ATTR_TIMESTAMP,
    DOMAIN,
    SERVICE_ADD_RECORD,
    SERVICE_EXPORT_RECORDS,
    SERVICE_IMPORT_RECORDS,
)
from .csv_transfer import build_export_csv, parse_import_csv
from .media_store import (
    allowed_source_roots,
    validate_source_path,
    validate_write_target_path,
)
from .record_view import to_public_record
from .schema import validate_record_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from .models import RecordType
    from .runtime_data import CustomRecordsRuntimeData

# CSV export/import `path` service params are restricted to this extension,
# same allow-listed-root protection as IMAGE field source paths.
CSV_EXTENSIONS = {".csv"}

SERVICE_ADD_RECORD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_RECORD_TYPE): cv.string,
        vol.Required(ATTR_FIELDS): dict,
        vol.Optional(ATTR_TIMESTAMP): cv.datetime,
    }
)

SERVICE_EXPORT_RECORDS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_RECORD_TYPE): cv.string,
        vol.Optional(ATTR_PATH): cv.string,
        vol.Optional(ATTR_INCLUDE_ID, default=True): cv.boolean,
    }
)

SERVICE_IMPORT_RECORDS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_RECORD_TYPE): cv.string,
        vol.Optional(ATTR_PATH): cv.string,
        vol.Optional(ATTR_CONTENT): cv.string,
    }
)


def _get_runtime_data(hass: HomeAssistant) -> CustomRecordsRuntimeData:
    """Return the runtime data for the (single) loaded config entry."""
    entries = hass.config_entries.async_entries(DOMAIN)
    loaded = [entry for entry in entries if entry.state is ConfigEntryState.LOADED]
    if not loaded:
        msg = "Custom Records is not set up"
        raise ServiceValidationError(msg)
    return loaded[0].runtime_data


def _get_record_type(
    runtime_data: CustomRecordsRuntimeData, record_type_id: str
) -> RecordType:
    """Return the RecordType for record_type_id, or raise ServiceValidationError."""
    record_type = runtime_data.record_types.get(record_type_id)
    if record_type is None:
        msg = f"Unknown record_type '{record_type_id}'"
        raise ServiceValidationError(msg)
    return record_type


def _require_str(value: str | None) -> str:
    """
    Narrow an Optional[str] known to be non-None at this point in the flow.

    Used after a runtime check (e.g. the path/content XOR check below) has
    already guaranteed the value is set, so the type checker can narrow it
    without a bare `assert` (stripped under `python -O`, flagged by S101).
    """
    if value is None:
        msg = "Expected a value to be set at this point"
        raise ValueError(msg)
    return value


def _export_to_path(hass: HomeAssistant, path: str, csv_text: str) -> str:
    """Validate `path` and write csv_text to it. Runs in the executor."""
    target = validate_write_target_path(
        path, allowed_source_roots(hass), CSV_EXTENSIONS, "CSV"
    )
    target.write_text(csv_text, encoding="utf-8")
    return str(target)


def _import_from_path(hass: HomeAssistant, path: str) -> str:
    """Validate `path` and read CSV text from it. Runs in the executor."""
    source = validate_source_path(
        path, allowed_source_roots(hass), CSV_EXTENSIONS, "CSV"
    )
    return source.read_text(encoding="utf-8")


async def _async_add_record(call: ServiceCall) -> ServiceResponse:
    """Handle the custom_records.add_record service call."""
    runtime_data = _get_runtime_data(call.hass)
    record_type_id = call.data[ATTR_RECORD_TYPE]
    record_type = _get_record_type(runtime_data, record_type_id)

    try:
        validated_fields = validate_record_data(record_type, call.data[ATTR_FIELDS])
    except vol.Invalid as err:
        raise ServiceValidationError(str(err)) from err

    try:
        record = await runtime_data.media_store.async_add_record_with_images(
            runtime_data.storage,
            record_type,
            validated_fields,
            call.data.get(ATTR_TIMESTAMP),
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    return to_public_record(record, record_type)


async def _async_export_records(call: ServiceCall) -> ServiceResponse:
    """Handle the custom_records.export_records service call."""
    runtime_data = _get_runtime_data(call.hass)
    record_type_id = call.data[ATTR_RECORD_TYPE]
    record_type = _get_record_type(runtime_data, record_type_id)

    records = await runtime_data.storage.async_list_records(record_type_id)
    csv_text = build_export_csv(
        record_type, records, include_id=call.data[ATTR_INCLUDE_ID]
    )

    path = call.data.get(ATTR_PATH)
    if path is None:
        return {"csv": csv_text}

    try:
        written_path = await call.hass.async_add_executor_job(
            _export_to_path, call.hass, path, csv_text
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    return {"path": written_path}


async def _async_import_records(call: ServiceCall) -> ServiceResponse:
    """Handle the custom_records.import_records service call."""
    runtime_data = _get_runtime_data(call.hass)
    record_type_id = call.data[ATTR_RECORD_TYPE]
    record_type = _get_record_type(runtime_data, record_type_id)

    path = call.data.get(ATTR_PATH)
    content = call.data.get(ATTR_CONTENT)
    if (path is None) == (content is None):
        msg = "Provide exactly one of 'path' or 'content'"
        raise ServiceValidationError(msg)

    if content is not None:
        csv_text = content
    else:
        try:
            csv_text = await call.hass.async_add_executor_job(
                _import_from_path, call.hass, _require_str(path)
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    parse_result = parse_import_csv(record_type, csv_text)
    summary = await runtime_data.storage.async_import_records(
        record_type_id, parse_result.rows
    )
    result: dict[str, Any] = {
        "imported": summary.imported,
        "skipped_duplicate": summary.skipped_duplicate,
        "errors": parse_result.errors,
    }
    return result


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the custom_records services (module-level, hass-wide)."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_RECORD):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_RECORD,
        _async_add_record,
        schema=SERVICE_ADD_RECORD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_RECORDS,
        _async_export_records,
        schema=SERVICE_EXPORT_RECORDS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_RECORDS,
        _async_import_records,
        schema=SERVICE_IMPORT_RECORDS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
