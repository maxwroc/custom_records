"""Tests for the config flow and the record_type subentry flow."""

# pyright: reportArgumentType=false
# pyright: reportOptionalSubscript=false
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import FormData
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.custom_records.const import DOMAIN, SUBENTRY_TYPE_RECORD_TYPE

from .conftest import BP_RECORD_TYPE, async_setup_entry_with_types

if TYPE_CHECKING:
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
