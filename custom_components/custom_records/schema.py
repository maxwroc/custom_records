"""Build dynamic voluptuous schemas for validating record field data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from .const import FieldType

if TYPE_CHECKING:
    from .models import FieldDefinition, RecordType


def _validator_for_field(
    field_def: FieldDefinition, *, enforce_options: bool = True
) -> Any:
    """
    Return the voluptuous validator for a single field definition.

    `enforce_options=False` treats select fields as plain (list-of-)strings
    instead of restricting them to the field's configured `options` - used
    for CSV import, where `options` is only a UI convenience (dropdown to
    avoid typos), not a hard data constraint (see `build_import_field_validators`).
    """
    if field_def.type is FieldType.SINGLE_SELECT:
        return vol.In(field_def.options or []) if enforce_options else cv.string
    if field_def.type is FieldType.MULTI_SELECT:
        return [vol.In(field_def.options or [])] if enforce_options else [cv.string]

    if field_def.type is FieldType.IMAGE:
        # IMAGE accepts either shape, both handed off to media_store.py:
        # - a filesystem path string (service/automation calls ->
        #   MediaStore.async_store_image), or
        # - a {"file_id": ...} object referencing a file already staged via
        #   HA's file_upload component (the card's upload UI ->
        #   MediaStore.async_store_uploaded_image).
        return vol.Any(cv.string, vol.Schema({vol.Required("file_id"): str}))

    simple_validators: dict[FieldType, Any] = {
        FieldType.NUMBER: vol.Coerce(float),
        FieldType.BOOLEAN: cv.boolean,
        FieldType.DATETIME: cv.datetime,
        FieldType.TEXT: cv.string,
        FieldType.LONG_TEXT: cv.string,
    }
    return simple_validators[field_def.type]


def build_fields_schema(record_type: RecordType) -> vol.Schema:
    """Build a voluptuous Schema validating the 'fields' dict of a record type."""
    schema_dict: dict[Any, Any] = {}
    for field_def in record_type.fields:
        validator = _validator_for_field(field_def)
        key: Any
        if field_def.required:
            key = vol.Required(field_def.key)
        elif field_def.default is not None:
            key = vol.Optional(field_def.key, default=field_def.default)
        else:
            key = vol.Optional(field_def.key)
        schema_dict[key] = validator
    return vol.Schema(schema_dict)


def build_import_field_validators(record_type: RecordType) -> dict[str, Any]:
    """
    Build a per-field-key validator map for CSV import (csv_transfer.py).

    Returns one callable validator per non-IMAGE field (IMAGE fields are
    already-finalized reference objects on import, not a source path to
    validate - see csv_transfer.py). Each validator is wrapped in
    `vol.Schema(...)` so it's directly callable on a bare value (e.g.
    multi_select's `_validator_for_field` returns a bare list `[vol.In(...)]`,
    which is only usable as a dict-schema value, not callable on its own,
    unless it's itself wrapped by a `vol.Schema`).

    Select fields (`single_select`/`multi_select`) are validated WITHOUT
    enforcing the field's configured `options` list: `options` is only a
    UI convenience to reduce typos when adding a record through the
    card/service, not a hard data constraint, so importing a CSV (e.g. a
    backup taken before options were edited/removed) must not reject
    otherwise-valid rows just because a value is no longer in the current
    list.
    """
    return {
        field_def.key: vol.Schema(
            _validator_for_field(field_def, enforce_options=False)
        )
        for field_def in record_type.fields
        if field_def.type is not FieldType.IMAGE
    }


def validate_filter_value(field_def: FieldDefinition, raw_value: Any) -> Any:
    """
    Coerce/validate a single filter literal (websocket_api.py/filter_query.py).

    Reuses the same per-field validator a stored field value would go
    through, so e.g. a NUMBER field's filter value "120" coerces to 120.0
    before comparing - keeps "what you can filter on" consistent with "what
    you can store". Raises vol.Invalid on failure.

    MULTI_SELECT is special-cased: `_validator_for_field` returns a
    LIST-shaped validator (`[vol.In(options)]`) for validating the full
    stored list, but a filter literal for a multi_select field is always a
    SINGLE value to check list membership for (see filter_query.py) - reusing
    the list validator as-is would incorrectly try to validate the raw value
    itself as a sequence of items.
    """
    if field_def.type is FieldType.MULTI_SELECT:
        return vol.Schema(vol.In(field_def.options or []))(raw_value)
    return vol.Schema(_validator_for_field(field_def))(raw_value)


def validate_record_data(
    record_type: RecordType, data: dict[str, Any]
) -> dict[str, Any]:
    """
    Validate and coerce a record's field data against its record type.

    Raises vol.Invalid on failure.
    """
    schema = build_fields_schema(record_type)
    return cast("dict[str, Any]", schema(data))
