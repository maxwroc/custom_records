"""Tests for the config flow and the record_type subentry flow."""

# pyright: reportArgumentType=false
# pyright: reportOptionalSubscript=false
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import voluptuous as vol
from aiohttp import FormData
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.custom_records.config_flow import (
    FieldsDefinitionError,
    _parse_fields_definition,
)
from custom_components.custom_records.const import (
    DB_FILENAME_TEMPLATE,
    DOMAIN,
    SUBENTRY_TYPE_RECORD_TYPE,
)

from .conftest import BP_RECORD_TYPE, async_setup_entry_with_types, make_source_image

if TYPE_CHECKING:
    from typing import Any

    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator


def _subentry_id(entry: config_entries.ConfigEntry, unique_id: str) -> str:
    """Look up a subentry's internal subentry_id by its unique_id (record type id)."""
    return next(
        se.subentry_id for se in entry.subentries.values() if se.unique_id == unique_id
    )


async def _init_add_flow(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> config_entries.SubentryFlowResult:
    """Start the 'add a new record type' subentry flow."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_RECORD_TYPE),
        context={"source": config_entries.SOURCE_USER, "entry_id": entry.entry_id},
    )


async def _init_reconfigure_flow(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, unique_id: str
) -> config_entries.SubentryFlowResult:
    """Start the 'reconfigure an existing record type' subentry flow."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_RECORD_TYPE),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
            "subentry_id": _subentry_id(entry, unique_id),
        },
    )


async def test_user_flow_creates_single_entry(hass: HomeAssistant) -> None:
    """The user step has nothing to configure and just creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Custom Records"
    assert result["result"].domain == "custom_records"


async def test_add_record_type_and_field(hass: HomeAssistant) -> None:
    """Adding a record type then a field creates a subentry with that data."""
    entry = await async_setup_entry_with_types(hass)

    result = await _init_add_flow(hass, entry)
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Blood Pressure"}
    )
    assert result["step_id"] == "add_field"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "label": "Systolic",
            "key": "systolic",
            "type": "number",
            "required": True,
            "add_another": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["unique_id"] == "blood_pressure"
    assert result["data"]["fields"][0]["key"] == "systolic"

    await hass.async_block_till_done()
    assert "blood_pressure" in entry.runtime_data.record_types


async def test_add_field_generates_key_from_label(hass: HomeAssistant) -> None:
    """Leaving 'key' blank generates one from the label (like HA entity ids)."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Blood Pressure"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "label": "Systolic Pressure!",
            "type": "number",
            "required": True,
            "add_another": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["fields"][0]["key"] == "systolic_pressure"
    assert result["data"]["fields"][0]["label"] == "Systolic Pressure!"
    await hass.async_block_till_done()


async def test_add_field_requires_label(hass: HomeAssistant) -> None:
    """A blank field name (label) is rejected."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Blood Pressure"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"label": "", "type": "number", "required": False, "add_another": False},
    )
    assert result["step_id"] == "add_field"
    assert result["errors"] == {"label": "label_required"}


async def test_add_field_rejects_reserved_key(hass: HomeAssistant) -> None:
    """Reserved field keys (id/timestamp/record_type) are rejected with an error."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Test"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "label": "Timestamp",
            "key": "timestamp",
            "type": "text",
            "required": False,
            "add_another": False,
        },
    )
    assert result["step_id"] == "add_field"
    assert result["errors"] == {"key": "reserved_key"}


async def test_add_field_requires_options_for_select_types(hass: HomeAssistant) -> None:
    """single_select/multi_select fields require at least one option."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Mood"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "label": "Mood",
            "key": "mood",
            "type": "single_select",
            "required": False,
            "add_another": False,
        },
    )
    assert result["errors"] == {"options": "options_required"}


@pytest.mark.parametrize(
    "definition",
    [
        '[{"label": "Systolic Pressure!", "type": "number", "required": true}]',
        "- label: Systolic Pressure!\n  type: number\n  required: true",
        '{"fields": [{"label": "Systolic Pressure!", "type": "number",'
        ' "required": true}], "id": "ignored", "sql_table": "ignored"}',
        "fields:\n  - label: Systolic Pressure!\n    type: number\n    required: true",
    ],
)
async def test_paste_definition_creates_record_type(
    hass: HomeAssistant, definition: str
) -> None:
    """Paste-only JSON/YAML submission persists and registers the same type."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    assert result["step_id"] == "user"
    schema = result["data_schema"]
    assert schema is not None
    marker = next(key for key in schema.schema if key == "fields_definition")
    assert isinstance(marker, vol.Optional)
    assert schema.schema[marker].config["multiline"] is True
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Blood Pressure", "fields_definition": definition},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["unique_id"] == "blood_pressure"
    assert result["data"]["sql_table"] == "records_blood_pressure"
    assert result["data"]["fields"] == [
        {
            "key": "systolic_pressure",
            "label": "Systolic Pressure!",
            "type": "number",
            "required": True,
            "unit": None,
            "default": None,
            "options": None,
            "sql_column": "systolic_pressure",
        }
    ]
    await hass.async_block_till_done()
    assert list(entry.runtime_data.record_types) == ["blood_pressure"]
    assert len(entry.subentries) == 1
    record = await entry.runtime_data.storage.async_add_record(
        "blood_pressure", {"systolic_pressure": 120}
    )
    assert record["d"] == {"systolic_pressure": 120}


@pytest.mark.parametrize(
    ("field", "expected_default"),
    [
        ({"key": "value", "default": "hello"}, "hello"),
        ({"key": "value", "type": "long_text", "default": "long\ntext"}, "long\ntext"),
        ({"key": "value", "type": "number", "default": 120.5}, 120.5),
        ({"key": "value", "type": "boolean", "default": False}, False),
        (
            {
                "key": "value",
                "type": "datetime",
                "default": "2026-10-07T12:00:00+00:00",
            },
            "2026-10-07T12:00:00+00:00",
        ),
        (
            {
                "key": "value",
                "type": "single_select",
                "options": " happy , sad ",
                "default": "happy",
            },
            "happy",
        ),
        (
            {
                "key": "value",
                "type": "multi_select",
                "options": [" happy ", " sad "],
                "default": ["happy", "sad"],
            },
            ["happy", "sad"],
        ),
        (
            {"key": "value", "type": "image", "default": "/config/photo.jpg"},
            "/config/photo.jpg",
        ),
        ({"key": "value", "type": "number", "default": None}, None),
    ],
)
def test_definition_field_types_and_defaults(
    field: dict[str, Any], expected_default: Any
) -> None:
    """Defaults preserve their JSON-compatible values for every field type."""
    parsed = _parse_fields_definition(json.dumps([field]))[0]
    assert parsed.default == expected_default
    assert parsed.label == "value"
    assert parsed.type.value == field.get("type", "text")
    assert parsed.required is False
    if "options" in field:
        assert parsed.options == ["happy", "sad"]


def test_definition_aliases_order_and_physical_names() -> None:
    """Generate logical keys and never import arbitrary physical SQL names."""
    fields = _parse_fields_definition(
        json.dumps(
            [
                {"name": "First field", "key": "", "sql_column": "arbitrary"},
                {"label": "Second", "name": "Ignored", "key": "custom", "unit": "kg"},
                {"key": "third", "unit": None},
            ]
        )
    )
    assert [field.key for field in fields] == ["first_field", "custom", "third"]
    assert [field.label for field in fields] == ["First field", "Second", "third"]
    assert [field.sql_column for field in fields] == ["first_field", "custom", "third"]
    assert fields[1].unit == "kg"


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ("[", "Invalid JSON/YAML"),
        ("hello", "nonempty field list"),
        ("null", "nonempty field list"),
        ("[]", "nonempty field list"),
        ('{"fields": []}', "nonempty field list"),
        ('{"name": "Only a name"}', "nonempty field list"),
        ('{"fields": {}}', "nonempty field list"),
        ("[123]", "Field 1: expected a field object"),
        ("[{}]", "Field 1: Provide a nonempty"),
        ('[{"key": "valid"}, {"key": "valid"}]', "Field 2 (valid): Duplicate"),
        ('[{"label": "My Field!"}, {"label": "My Field"}]', "Duplicate field key"),
        ("- &loop\n  key: value\n  default: *loop", "Default must be a string"),
        ("- key: value\n  type: datetime\n  default: 2026-10-07", "quote datetime"),
        (
            "- key: value\n  type: datetime\n  default: 2026-10-07T12:00:00Z",
            "quote datetime",
        ),
        ("!!python/object/apply:os.getcwd []", "Invalid JSON/YAML"),
    ],
)
def test_definition_rejects_invalid_structure(definition: str, message: str) -> None:
    """Invalid syntax, shapes, aliases, and duplicates produce contextual errors."""
    with pytest.raises(FieldsDefinitionError, match=re.escape(message)):
        _parse_fields_definition(definition)


@pytest.mark.parametrize(
    "field",
    [
        {"key": "timestamp"},
        {"key": "select"},
        {"key": "Invalid"},
        {"key": "has__double"},
        {"key": "a" * 64},
        {"key": ""},
        {"key": 123},
        {"key": None},
        {"key": "value", "label": ""},
        {"key": "value", "label": None},
        {"key": "value", "name": False},
        {"key": "value", "type": "unknown"},
        {"key": "value", "type": 3},
        {"key": "value", "required": "false"},
        {"key": "value", "required": 1},
        {"key": "value", "unit": 3},
        {"key": "value", "require": True},
        {"key": "value", "options": {}},
        {"key": "value", "options": [3]},
        {"key": "value", "type": "single_select"},
        {"key": "value", "type": "single_select", "options": []},
        {"key": "value", "type": "single_select", "options": " , "},
        {"key": "value", "type": "multi_select", "options": ["a", " a "]},
        {"key": "value", "type": "multi_select", "options": ["a", ""]},
        {"key": "value", "type": "single_select", "options": "a, a"},
        {"key": "value", "default": []},
        {"key": "value", "type": "long_text", "default": {}},
        {"key": "value", "type": "boolean", "default": "true"},
        {"key": "value", "type": "number", "default": True},
        {"key": "value", "type": "number", "default": "120"},
        {"key": "value", "type": "number", "default": float("nan")},
        {"key": "value", "type": "number", "default": float("inf")},
        {"key": "value", "type": "number", "default": 10**400},
        {"key": "value", "type": "datetime", "default": "not a date"},
        {"key": "value", "type": "datetime", "default": "2026-10-07T12:00:00"},
        {"key": "value", "type": "datetime", "default": 123},
        {"key": "value", "type": "image", "default": {"file_id": "123"}},
        {"key": "value", "type": "image", "default": ""},
        {"key": "value", "type": "single_select", "options": ["a"], "default": "b"},
        {"key": "value", "type": "single_select", "options": ["a"], "default": []},
        {"key": "value", "type": "multi_select", "options": ["a"], "default": ["b"]},
        {"key": "value", "type": "multi_select", "options": ["a"], "default": ["a", 1]},
    ],
)
def test_definition_rejects_invalid_field(field: dict[str, Any]) -> None:
    """Reject malformed attributes without coercing them or leaking exceptions."""
    with pytest.raises(FieldsDefinitionError, match="Field 1"):
        _parse_fields_definition(json.dumps([field]))


async def test_invalid_definition_preserves_name_and_input(
    hass: HomeAssistant,
) -> None:
    """An invalid later field has no side effects and allows correction."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    definition = '[{"key": "valid"}, {"key": "invalid", "type": "unknown"}]'
    with patch.object(entry.runtime_data.storage, "async_ensure_record_type") as ensure:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"name": "Test", "fields_definition": definition}
        )
        ensure.assert_not_called()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"fields_definition": "invalid_definition"}
    assert "Field 2 (invalid)" in result["description_placeholders"]["error"]
    schema = result["data_schema"]
    assert schema is not None
    marker = next(
        key
        for key in schema.schema
        if isinstance(key, vol.Marker) and key == "fields_definition"
    )
    assert marker.description["suggested_value"] == definition
    name_marker = next(
        key for key in schema.schema if isinstance(key, vol.Marker) and key == "name"
    )
    assert name_marker.description["suggested_value"] == "Test"
    assert not entry.subentries
    assert not entry.runtime_data.record_types
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Test", "fields_definition": '[{"key": "valid"}]'}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [field["key"] for field in result["data"]["fields"]] == ["valid"]
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("", "name_required"),
        (" \n ", "name_required"),
        ("Blood Pressure", "already_exists"),
    ],
)
async def test_definition_requires_valid_unique_name(
    hass: HomeAssistant, name: str, error: str
) -> None:
    """A pasted definition must not bypass name validation or uniqueness."""
    record_type = {**BP_RECORD_TYPE, "id": "blood_pressure"}
    entry = await async_setup_entry_with_types(hass, [record_type])
    result = await _init_add_flow(hass, entry)
    definition = '[{"key": "value"}]'
    with patch.object(entry.runtime_data.storage, "async_ensure_record_type") as ensure:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"name": name, "fields_definition": definition}
        )
        ensure.assert_not_called()
    assert result["step_id"] == "user"
    assert result["errors"] == {"name": error}
    assert len(entry.subentries) == 1


@pytest.mark.parametrize("manual", [{"fields_definition": " \n "}, {}])
async def test_blank_definition_continues_manual_creation(
    hass: HomeAssistant, manual: dict[str, Any]
) -> None:
    """Omitted or whitespace-only definitions retain the manual field wizard."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Test", **manual}
    )
    assert result["step_id"] == "add_field"
    schema = result["data_schema"]
    assert schema is not None
    assert all(key != "fields_definition" for key in schema.schema)
    label_marker = next(key for key in schema.schema if key == "label")
    assert isinstance(label_marker, vol.Required)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"label": "", "type": "number"}
    )
    assert result["errors"] == {"label": "label_required"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"label": "Buffered", "type": "number", "add_another": True},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"label": "Value", "type": "number"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["fields"][0]["type"] == "number"
    assert [field["key"] for field in result["data"]["fields"]] == ["buffered", "value"]
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "submission",
    [
        {"label": "Value", "type": "number"},
        {"name": "Test", "fields_definition": '[{"key": "value", "type": "number"}]'},
    ],
)
async def test_creation_database_failure_can_retry(
    hass: HomeAssistant, submission: dict[str, Any]
) -> None:
    """A failed candidate is not appended to the buffer on resubmission."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    if "label" in submission:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"name": "Test"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"label": "Buffered", "type": "text", "add_another": True},
        )
    with patch.object(
        entry.runtime_data.storage,
        "async_ensure_record_type",
        side_effect=sqlite3.OperationalError("test failure"),
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], submission
        )
    assert result["errors"] == {"base": "database_error"}
    assert not entry.subentries
    if "label" in submission:
        assert result["step_id"] == "add_field"
        assert result["description_placeholders"]["count"] == "1"
    else:
        assert result["step_id"] == "user"
        schema = result["data_schema"]
        assert schema is not None
        marker = next(
            key
            for key in schema.schema
            if isinstance(key, vol.Marker) and key == "fields_definition"
        )
        assert marker.description["suggested_value"] == submission["fields_definition"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], submission
    )
    expected_keys = ["buffered", "value"] if "label" in submission else ["value"]
    assert [field["key"] for field in result["data"]["fields"]] == expected_keys
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1


async def test_creation_schema_failure_rolls_back(hass: HomeAssistant) -> None:
    """Failure after CREATE TABLE leaves no partial schema before a corrected retry."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    with patch(
        "custom_components.custom_records.store._index_sql", return_value="INVALID SQL"
    ):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"name": "Test", "fields_definition": '[{"key": "old"}]'},
        )
    assert result["errors"] == {"base": "database_error"}
    assert not entry.subentries
    assert not entry.runtime_data.record_types
    tables = await hass.async_add_executor_job(_definition_tables, hass, entry.entry_id)
    assert tables == []
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Test",
            "fields_definition": '[{"key": "corrected", "required": true}]',
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert [field.key for field in entry.runtime_data.record_types["test"].fields] == [
        "corrected"
    ]


def _definition_tables(hass: HomeAssistant, entry_id: str) -> list[tuple[str]]:
    """Inspect the test database without using storage internals."""
    path = hass.config.path(
        ".storage", DOMAIN, DB_FILENAME_TEMPLATE.format(entry_id=entry_id)
    )
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'records_test'"
        ).fetchall()


async def test_image_definition_default_requires_existing_path(
    hass: HomeAssistant,
) -> None:
    """Image defaults use the existing path validator before schema creation."""
    entry = await async_setup_entry_with_types(hass)
    result = await _init_add_flow(hass, entry)
    field = {"key": "photo", "type": "image", "default": "/outside/missing.jpg"}
    with patch.object(entry.runtime_data.storage, "async_ensure_record_type") as ensure:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"name": "Test", "fields_definition": json.dumps([field])},
        )
        ensure.assert_not_called()
    assert result["errors"] == {"fields_definition": "invalid_definition"}
    assert "Field 1 (photo)" in result["description_placeholders"]["error"]
    field["default"] = str(make_source_image(hass))
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Test", "fields_definition": json.dumps([field])}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["fields"][0]["default"] == field["default"]
    await hass.async_block_till_done()


async def test_add_record_type_name_collision(hass: HomeAssistant) -> None:
    """A name that slugifies to an existing record type id is rejected."""
    entry = await async_setup_entry_with_types(hass)

    result = await _init_add_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Blood Pressure"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "label": "Systolic",
            "key": "systolic",
            "type": "number",
            "required": True,
            "add_another": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    result = await _init_add_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Blood Pressure"}
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"name": "already_exists"}


async def test_reconfigure_menu(hass: HomeAssistant) -> None:
    """Reconfiguring a record type shows the expected management menu."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "manage_fields",
        "reconfigure_add_field",
        "set_retention",
        "export_data",
        "import_data",
    }


async def test_reconfigure_add_field(hass: HomeAssistant) -> None:
    """Adding an optional field via reconfigure appends to the field list."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_add_field"}
    )
    schema = result["data_schema"]
    assert schema is not None
    assert all(key != "fields_definition" for key in schema.schema)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "label": "Diastolic",
            "key": "diastolic",
            "type": "number",
            "required": False,
            "add_another": False,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    record_type = entry.runtime_data.record_types["bp"]
    assert {f.key for f in record_type.fields} == {"systolic", "diastolic"}


async def test_reconfigure_add_field_rejects_required(hass: HomeAssistant) -> None:
    """A required field can only be added while creating a NEW record type."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_add_field"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "label": "Diastolic",
            "key": "diastolic",
            "type": "number",
            "required": True,
            "add_another": False,
        },
    )
    assert result["step_id"] == "reconfigure_add_field"
    assert result["errors"] == {"required": "required_not_allowed_on_existing_type"}


async def test_edit_field_label(hass: HomeAssistant) -> None:
    """Editing a field's label leaves its key (and stored data) untouched."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manage_fields"}
    )
    assert result["step_id"] == "manage_fields"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"field_key": "systolic"}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"edit_field_label"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "edit_field_label"}
    )
    assert result["step_id"] == "edit_field_label"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"label": "Systolic (mmHg)"}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    record_type = entry.runtime_data.record_types["bp"]
    field = record_type.get_field("systolic")
    assert field.label == "Systolic (mmHg)"


async def test_edit_field_preserves_physical_sql_column(hass: HomeAssistant) -> None:
    """Mutable field metadata never resets its immutable physical SQL mapping."""
    aliased_type = {
        **BP_RECORD_TYPE,
        "fields": [{**BP_RECORD_TYPE["fields"][0], "sql_column": "physical_value"}],
    }
    entry = await async_setup_entry_with_types(hass, [aliased_type])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manage_fields"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"field_key": "systolic"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "edit_field_label"}
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"label": "Updated"}
    )
    await hass.async_block_till_done()

    field = entry.runtime_data.record_types["bp"].get_field("systolic")
    assert field.sql_column == "physical_value"


async def test_edit_select_options_add_remove_rename_reorder(
    hass: HomeAssistant,
) -> None:
    """The full option list can be edited: add, remove, rename, and reorder."""
    mood_type = {
        **BP_RECORD_TYPE,
        "id": "mood",
        "name": "Mood",
        "fields": [
            {
                "key": "mood",
                "label": "Mood",
                "type": "single_select",
                "required": False,
                "unit": None,
                "default": None,
                "options": ["happy", "sad"],
                "sql_column": "physical_mood",
            }
        ],
    }
    entry = await async_setup_entry_with_types(hass, [mood_type])
    result = await _init_reconfigure_flow(hass, entry, "mood")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manage_fields"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"field_key": "mood"}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"edit_field_label", "edit_select_options"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "edit_select_options"}
    )
    assert result["step_id"] == "edit_select_options"

    # Drop "sad", rename "happy" -> "glad", add "excited", and reorder.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"options": "excited, glad"}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    field = entry.runtime_data.record_types["mood"].get_field("mood")
    assert field.options == ["excited", "glad"]
    assert field.sql_column == "physical_mood"


async def test_edit_select_options_rejects_empty(hass: HomeAssistant) -> None:
    """An empty option list is rejected."""
    mood_type = {
        **BP_RECORD_TYPE,
        "id": "mood",
        "name": "Mood",
        "fields": [
            {
                "key": "mood",
                "label": "Mood",
                "type": "single_select",
                "required": False,
                "unit": None,
                "default": None,
                "options": ["happy", "sad"],
            }
        ],
    }
    entry = await async_setup_entry_with_types(hass, [mood_type])
    result = await _init_reconfigure_flow(hass, entry, "mood")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manage_fields"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"field_key": "mood"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "edit_select_options"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"options": "  ,  "}
    )
    assert result["step_id"] == "edit_select_options"
    assert result["errors"] == {"options": "options_required"}


async def test_edit_select_options_rejects_duplicate(hass: HomeAssistant) -> None:
    """Duplicate values in the edited list are rejected."""
    mood_type = {
        **BP_RECORD_TYPE,
        "id": "mood",
        "name": "Mood",
        "fields": [
            {
                "key": "mood",
                "label": "Mood",
                "type": "single_select",
                "required": False,
                "unit": None,
                "default": None,
                "options": ["happy", "sad"],
            }
        ],
    }
    entry = await async_setup_entry_with_types(hass, [mood_type])
    result = await _init_reconfigure_flow(hass, entry, "mood")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manage_fields"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"field_key": "mood"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "edit_select_options"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"options": "happy, happy"}
    )
    assert result["step_id"] == "edit_select_options"
    assert result["errors"] == {"options": "duplicate_option"}


async def test_edit_select_options_prunes_single_default(hass: HomeAssistant) -> None:
    """Removing/renaming the value used as a single-select default clears it."""
    mood_type = {
        **BP_RECORD_TYPE,
        "id": "mood",
        "name": "Mood",
        "fields": [
            {
                "key": "mood",
                "label": "Mood",
                "type": "single_select",
                "required": False,
                "unit": None,
                "default": "sad",
                "options": ["happy", "sad"],
            }
        ],
    }
    entry = await async_setup_entry_with_types(hass, [mood_type])
    result = await _init_reconfigure_flow(hass, entry, "mood")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manage_fields"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"field_key": "mood"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "edit_select_options"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"options": "happy, glad"}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    field = entry.runtime_data.record_types["mood"].get_field("mood")
    assert field.options == ["happy", "glad"]
    assert field.default is None


async def test_edit_select_options_prunes_multi_default(hass: HomeAssistant) -> None:
    """Removed values are dropped from a multi-select default; kept ones remain."""
    tags_type = {
        **BP_RECORD_TYPE,
        "id": "tags",
        "name": "Tags",
        "fields": [
            {
                "key": "tags",
                "label": "Tags",
                "type": "multi_select",
                "required": False,
                "unit": None,
                "default": ["a", "b"],
                "options": ["a", "b", "c"],
            }
        ],
    }
    entry = await async_setup_entry_with_types(hass, [tags_type])
    result = await _init_reconfigure_flow(hass, entry, "tags")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manage_fields"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"field_key": "tags"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "edit_select_options"}
    )
    # Drop "b" (used in default) and "c"; keep "a".
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"options": "a"}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    field = entry.runtime_data.record_types["tags"].get_field("tags")
    assert field.options == ["a"]
    assert field.default == ["a"]


async def test_set_retention_values(hass: HomeAssistant) -> None:
    """Retention/max_records/warn_at can be set for an existing record type."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "set_retention"}
    )
    assert result["step_id"] == "set_retention"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"retention_days": 30, "max_records": 1000, "warn_at": 500}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    record_type = entry.runtime_data.record_types["bp"]
    assert record_type.retention_days == 30
    assert record_type.max_records == 1000
    assert record_type.warn_at == 500


async def test_set_retention_rejects_non_positive_values(
    hass: HomeAssistant,
) -> None:
    """Retention settings must be positive when configured."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "set_retention"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"retention_days": 0, "max_records": -1, "warn_at": 0}
    )

    assert result["step_id"] == "set_retention"
    assert result["errors"] == {
        "retention_days": "positive_integer",
        "max_records": "positive_integer",
        "warn_at": "positive_integer",
    }


async def test_export_data_returns_signed_download_url(hass: HomeAssistant) -> None:
    """export_data aborts with a signed download link reflecting include_id."""
    assert await async_setup_component(hass, "http", {})
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "export_data"}
    )
    assert result["step_id"] == "export_data"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"include_id": True}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "export_ready"
    download_url = result["description_placeholders"]["download_url"]
    assert f"/{DOMAIN}_export/{entry.entry_id}/bp" in download_url
    assert "include_id=true" in download_url
    assert "authSig=" in download_url


async def test_export_data_include_id_false_reflected_in_url(
    hass: HomeAssistant,
) -> None:
    """Unchecking include_id is reflected in the signed download link."""
    assert await async_setup_component(hass, "http", {})
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "export_data"}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"include_id": False}
    )

    assert "include_id=false" in result["description_placeholders"]["download_url"]


async def _upload_csv(client: ClientSessionGenerator, csv_text: str) -> str:
    """Upload a CSV file via the standard /api/file_upload endpoint; return file_id."""
    form = FormData()
    form.add_field(
        "file", csv_text.encode(), filename="import.csv", content_type="text/csv"
    )
    resp = await client.post("/api/file_upload", data=form)
    assert resp.status == 200
    return (await resp.json())["file_id"]


async def test_import_data_happy_path(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Uploading a CSV imports its rows and shows a summary abort."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])

    client = await hass_client()
    file_id = await _upload_csv(
        client, "id,timestamp,systolic\n,2026-01-01T10:00:00+00:00,120\n"
    )

    result = await _init_reconfigure_flow(hass, entry, "bp")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "import_data"}
    )
    assert result["step_id"] == "import_data"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"file": file_id}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "import_complete"
    assert result["description_placeholders"]["imported"] == "1"
    assert result["description_placeholders"]["skipped"] == "0"
    assert await entry.runtime_data.storage.async_record_count("bp") == 1


async def test_import_data_skips_duplicate_id_on_reimport(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Re-importing the same file (same id) is idempotent - skipped, not duplicated."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    csv_text = "id,timestamp,systolic\nfixed-id,2026-01-01T10:00:00+00:00,120\n"
    client = await hass_client()

    for _ in range(2):
        file_id = await _upload_csv(client, csv_text)
        result = await _init_reconfigure_flow(hass, entry, "bp")
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"next_step_id": "import_data"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"file": file_id}
        )

    assert result["description_placeholders"]["imported"] == "0"
    assert result["description_placeholders"]["skipped"] == "1"
    assert await entry.runtime_data.storage.async_record_count("bp") == 1
