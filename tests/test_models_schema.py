"""Tests for custom_records.models and schema.py validation."""

from __future__ import annotations

import pytest
import voluptuous as vol

from custom_components.custom_records.const import FieldType
from custom_components.custom_records.models import FieldDefinition, RecordType
from custom_components.custom_records.schema import validate_record_data


def test_field_definition_round_trip() -> None:
    """A FieldDefinition should survive a to_dict/from_dict round trip."""
    field = FieldDefinition(
        key="systolic", label="Systolic", type=FieldType.NUMBER, required=True
    )
    assert FieldDefinition.from_dict(field.to_dict()) == field


def test_select_field_requires_options() -> None:
    """Select-type fields must carry a non-empty options list."""
    with pytest.raises(ValueError, match="requires a non-empty options list"):
        FieldDefinition(key="mood", label="Mood", type=FieldType.SINGLE_SELECT)


def test_record_type_round_trip() -> None:
    """A RecordType (with fields) should survive a to_dict/from_dict round trip."""
    record_type = RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            FieldDefinition(key="systolic", label="Systolic", type=FieldType.NUMBER)
        ],
        retention_days=30,
    )
    assert RecordType.from_dict(record_type.to_dict()) == record_type


def _bp_record_type(*, required: bool = True) -> RecordType:
    return RecordType(
        id="bp",
        name="Blood Pressure",
        fields=[
            FieldDefinition(
                key="systolic",
                label="Systolic",
                type=FieldType.NUMBER,
                required=required,
            ),
            FieldDefinition(
                key="notes", label="Notes", type=FieldType.TEXT, required=False
            ),
        ],
    )


def test_validate_record_data_success() -> None:
    """Valid input is coerced according to each field's type."""
    result = validate_record_data(_bp_record_type(), {"systolic": "120", "notes": "ok"})
    assert result == {"systolic": 120.0, "notes": "ok"}


def test_validate_record_data_missing_required() -> None:
    """A missing required field raises vol.Invalid."""
    with pytest.raises(vol.Invalid):
        validate_record_data(_bp_record_type(), {})


def test_validate_record_data_unknown_field_rejected() -> None:
    """Fields not defined on the record type are rejected."""
    with pytest.raises(vol.Invalid):
        validate_record_data(_bp_record_type(required=False), {"unexpected": 1})


def test_validate_record_data_single_select() -> None:
    """single_select fields only accept one of the configured options."""
    record_type = RecordType(
        id="mood",
        name="Mood",
        fields=[
            FieldDefinition(
                key="mood",
                label="Mood",
                type=FieldType.SINGLE_SELECT,
                options=["good", "bad"],
            )
        ],
    )
    assert validate_record_data(record_type, {"mood": "good"}) == {"mood": "good"}
    with pytest.raises(vol.Invalid):
        validate_record_data(record_type, {"mood": "ok"})


def test_validate_record_data_multi_select() -> None:
    """multi_select fields accept a list of valid options."""
    record_type = RecordType(
        id="tags",
        name="Tags",
        fields=[
            FieldDefinition(
                key="tags",
                label="Tags",
                type=FieldType.MULTI_SELECT,
                options=["a", "b"],
            )
        ],
    )
    assert validate_record_data(record_type, {"tags": ["a", "b"]}) == {
        "tags": ["a", "b"]
    }
    with pytest.raises(vol.Invalid):
        validate_record_data(record_type, {"tags": ["c"]})
