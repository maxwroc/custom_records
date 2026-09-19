"""Config and record-type subentry flows for Custom Records."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    DOMAIN,
    EXPORT_URL_PREFIX,
    LOGGER,
    RESERVED_FIELD_KEYS,
    RESERVED_SQL_KEYWORDS,
    SELECT_FIELD_TYPES,
    SUBENTRY_TYPE_RECORD_TYPE,
    FieldType,
    is_valid_field_key,
    is_valid_record_type_id,
)
from .csv_transfer import parse_import_csv
from .models import FieldDefinition, RecordType
from .store import SchemaError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .store import ImportSummary


def _optional_int(value: Any) -> int | None:
    """Coerce a form value to int, treating blank/None as 'unset'."""
    if value in (None, ""):
        return None
    return int(value)


def _prune_default(field: FieldDefinition, options: list[str]) -> Any:
    """
    Drop default value(s) that are no longer among a select field's options.

    Removing or renaming an option can orphan the field's configured default;
    there is no separate default-edit step, so prune it here to keep the
    field definition valid. Single-select defaults are cleared, multi-select
    defaults keep only still-valid items.
    """
    default = field.default
    if default is None:
        return None
    if field.type is FieldType.MULTI_SELECT:
        if not isinstance(default, list):
            return default
        return [item for item in default if item in options]
    if field.type is FieldType.SINGLE_SELECT:
        return default if default in options else None
    return default


def _require_str(value: str | None) -> str:
    """
    Narrow an Optional[str] known to be non-None at this point in the flow.

    Used for values sourced from ConfigSubentry.unique_id (typed str | None
    generically, though our own record_type subentries always set it) and
    from this flow's own transient state (always set by an earlier step
    before these accessors are reached). Raises rather than using a bare
    `assert` (stripped under `python -O`, and flagged by ruff's S101).
    """
    if value is None:
        msg = "Expected a value to be set at this point in the flow"
        raise ValueError(msg)
    return value


def _require_import_summary(value: ImportSummary | None) -> ImportSummary:
    """Narrow the transient _import_summary, set by async_step_import_data."""
    if value is None:
        msg = "Expected an import summary to be set at this point in the flow"
        raise ValueError(msg)
    return value


def _read_uploaded_csv(hass: HomeAssistant, file_id: str) -> str:
    """Read and clean up an uploaded CSV file. Runs in the executor."""
    with process_uploaded_file(hass, file_id) as path:
        return path.read_text(encoding="utf-8")


def _field_schema() -> vol.Schema:
    """Build the "add a field" form schema, shared by the create/reconfigure flows."""
    return vol.Schema(
        {
            vol.Required("label"): str,
            vol.Optional("key"): str,
            vol.Required(
                "type", default=FieldType.NUMBER.value
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[t.value for t in FieldType],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional("required", default=False): bool,
            vol.Optional("unit"): str,
            vol.Optional("options"): str,
            vol.Optional("add_another", default=False): bool,
        },
    )


def _field_selector(fields: list[FieldDefinition]) -> selector.SelectSelector:
    """Build a SelectSelector listing fields as 'Label (key)', so the key is visible."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=f.key, label=f"{f.label} ({f.key})")
                for f in fields
            ]
        )
    )


class CustomRecordsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Config flow for Custom Records.

    There is nothing to configure upfront - this integration has no external
    device/account to connect to, so the flow simply creates the single entry.
    Record types are configured afterwards as config subentries (see
    RecordTypeSubentryFlow below), so each one shows up as its own visible,
    individually manageable row directly on the integration's card in
    Settings > Devices & Services, with built-in add/reconfigure/delete
    actions - no separate "Configure" options dialog needed.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial (and only) step."""
        if user_input is not None:
            return self.async_create_entry(title="Custom Records", data={})
        return self.async_show_form(step_id="user")

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Each record type is configured as a 'record_type' subentry."""
        del config_entry
        return {SUBENTRY_TYPE_RECORD_TYPE: RecordTypeSubentryFlow}


class RecordTypeSubentryFlow(config_entries.ConfigSubentryFlow):
    """
    Add or reconfigure a single record type (name, fields, retention).

    One subentry per record type. Adding/removing is handled by Home
    Assistant's own built-in subentry UI (a "+" button on the integration's
    card, and a per-row Delete action) - only the add wizard (this flow's
    `user` step) and the reconfigure menu (`reconfigure` step) need to be
    implemented here.
    """

    def __init__(self) -> None:
        """Initialize transient wizard state."""
        self._name: str | None = None
        self._type_id: str | None = None
        self._fields: list[FieldDefinition] = []
        self._field_buffer: list[FieldDefinition] = []
        self._editing_field_key: str | None = None
        self._import_summary: ImportSummary | None = None
        self._import_errors: list[dict[str, Any]] = []

    # -- shared helpers ---------------------------------------------------

    def _existing_type_ids(self) -> set[str]:
        return {
            subentry.unique_id
            for subentry in self._get_entry().subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_RECORD_TYPE
            and subentry.unique_id is not None
        }

    def _existing_keys(self) -> set[str]:
        return {f.key for f in (*self._fields, *self._field_buffer)}

    def _current_record_type(self) -> RecordType:
        subentry = self._get_reconfigure_subentry()
        return RecordType.from_subentry(
            _require_str(subentry.unique_id), subentry.title, dict(subentry.data)
        )

    def _parse_field_input(
        self, user_input: dict[str, Any], *, allow_required: bool = True
    ) -> tuple[FieldDefinition | None, dict[str, str]]:
        """Validate an add-field form submission; return (field_or_None, errors)."""
        errors: dict[str, str] = {}
        label = user_input["label"].strip()
        if not label:
            errors["label"] = "label_required"

        # An explicit key wins; otherwise generate one from the name, the same
        # way HA derives an entity id from a friendly name (lowercased,
        # non-alphanumeric runs collapsed to a single underscore).
        key = (user_input.get("key") or "").strip() or slugify(label, separator="_")
        if not errors:
            if not key:
                errors["key"] = "key_required"
            elif key in RESERVED_FIELD_KEYS or key in RESERVED_SQL_KEYWORDS:
                errors["key"] = "reserved_key"
            elif not is_valid_field_key(key):
                errors["key"] = "invalid_key"
            elif key in self._existing_keys():
                errors["key"] = "duplicate_key"

        if not errors and user_input.get("required") and not allow_required:
            errors["required"] = "required_not_allowed_on_existing_type"

        options: list[str] | None = None
        field_type = FieldType(user_input["type"])
        if not errors and field_type in SELECT_FIELD_TYPES:
            raw_options = user_input.get("options", "")
            options = [o.strip() for o in raw_options.split(",") if o.strip()]
            if not options:
                errors["options"] = "options_required"

        if errors:
            return None, errors
        return (
            FieldDefinition(
                key=key,
                label=label,
                type=field_type,
                required=user_input.get("required", False),
                unit=user_input.get("unit") or None,
                options=options,
            ),
            errors,
        )

    # -- create a new record type -----------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Ask for the new record type's name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["name"].strip()
            type_id = slugify(name, separator="_")
            if not name:
                errors["name"] = "name_required"
            elif not is_valid_record_type_id(type_id):
                errors["name"] = "invalid_key"
            elif type_id in self._existing_type_ids():
                errors["name"] = "already_exists"
            else:
                self._name = name
                self._type_id = type_id
                self._fields = []
                self._field_buffer = []
                return await self.async_step_add_field()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("name"): str}),
            errors=errors,
        )

    async def async_step_add_field(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Add a field to the record type currently being created."""
        errors: dict[str, str] = {}
        if user_input is not None:
            field, errors = self._parse_field_input(user_input)
            if field is not None:
                self._field_buffer.append(field)
                if user_input.get("add_another"):
                    return await self.async_step_add_field()
                record_type = RecordType(
                    id=_require_str(self._type_id),
                    name=_require_str(self._name),
                    fields=[*self._fields, *self._field_buffer],
                )
                try:
                    await (
                        self._get_entry().runtime_data.storage.async_ensure_record_type(
                            record_type
                        )
                    )
                except sqlite3.Error, SchemaError:
                    LOGGER.exception(
                        "Failed to create database schema for record type %s",
                        record_type.id,
                    )
                    errors["base"] = "database_error"
                    return self.async_show_form(
                        step_id="add_field",
                        data_schema=_field_schema(),
                        errors=errors,
                        description_placeholders={
                            "count": str(len(self._field_buffer))
                        },
                    )
                return self.async_create_entry(
                    title=record_type.name,
                    data=record_type.to_subentry_data(),
                    unique_id=record_type.id,
                )

        return self.async_show_form(
            step_id="add_field",
            data_schema=_field_schema(),
            errors=errors,
            description_placeholders={"count": str(len(self._field_buffer))},
        )

    # -- reconfigure an existing record type -------------------------------

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.SubentryFlowResult:
        """Show the menu of things that can be changed about this record type."""
        del user_input
        subentry = self._get_reconfigure_subentry()
        self._type_id = _require_str(subentry.unique_id)
        self._name = subentry.title
        self._fields = self._current_record_type().fields
        self._field_buffer = []
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                "manage_fields",
                "reconfigure_add_field",
                "set_retention",
                "export_data",
                "import_data",
            ],
            description_placeholders={"name": self._name, "key": self._type_id},
        )

    async def async_step_reconfigure_add_field(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """
        Add one or more new OPTIONAL fields to an existing record type.

        A required field can only be collected while a record type is first
        being created (async_step_add_field) - adding a required field to an
        already-populated table would leave existing rows without a value
        for it, so `_parse_field_input` rejects `required` here (plan_sql.md
        Phase 1 pt.6).
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            field, errors = self._parse_field_input(user_input, allow_required=False)
            if field is not None:
                self._field_buffer.append(field)
                if user_input.get("add_another"):
                    return await self.async_step_reconfigure_add_field()
                all_fields = [*self._fields, *self._field_buffer]
                record_type = replace(self._current_record_type(), fields=all_fields)
                try:
                    await (
                        self._get_entry().runtime_data.storage.async_ensure_record_type(
                            record_type
                        )
                    )
                except sqlite3.Error, SchemaError:
                    LOGGER.exception(
                        "Failed to add database columns for record type %s",
                        record_type.id,
                    )
                    errors["base"] = "database_error"
                    return self.async_show_form(
                        step_id="reconfigure_add_field",
                        data_schema=_field_schema(),
                        errors=errors,
                        description_placeholders={
                            "count": str(len(self._field_buffer))
                        },
                    )
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    data_updates={"fields": [f.to_dict() for f in all_fields]},
                )

        return self.async_show_form(
            step_id="reconfigure_add_field",
            data_schema=_field_schema(),
            errors=errors,
            description_placeholders={"count": str(len(self._field_buffer))},
        )

    async def async_step_manage_fields(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Pick which field to manage (edit label, change key, or delete)."""
        if user_input is not None:
            self._editing_field_key = user_input["field_key"]
            return await self.async_step_field_actions()

        return self.async_show_form(
            step_id="manage_fields",
            data_schema=vol.Schema(
                {vol.Required("field_key"): _field_selector(self._fields)}
            ),
        )

    async def async_step_field_actions(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.SubentryFlowResult:
        """Show what can be done with the field picked in manage_fields."""
        del user_input
        field = next(f for f in self._fields if f.key == self._editing_field_key)
        menu_options = ["edit_field_label"]
        if field.type in SELECT_FIELD_TYPES:
            menu_options.append("edit_select_options")
        return self.async_show_menu(
            step_id="field_actions",
            menu_options=menu_options,
            description_placeholders={"label": field.label, "key": field.key},
        )

    async def async_step_edit_field_label(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Change only a field's display label (its key is untouched)."""
        field = next(f for f in self._fields if f.key == self._editing_field_key)
        errors: dict[str, str] = {}
        if user_input is not None:
            new_label = user_input["label"].strip()
            if not new_label:
                errors["label"] = "label_required"
            else:
                updated_fields = [
                    FieldDefinition(
                        key=f.key,
                        label=new_label if f.key == field.key else f.label,
                        type=f.type,
                        required=f.required,
                        unit=f.unit,
                        default=f.default,
                        options=f.options,
                        sql_column=f.sql_column,
                    )
                    for f in self._fields
                ]
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    data_updates={"fields": [f.to_dict() for f in updated_fields]},
                )

        return self.async_show_form(
            step_id="edit_field_label",
            data_schema=vol.Schema({vol.Required("label", default=field.label): str}),
            errors=errors,
            description_placeholders={"key": field.key},
        )

    async def async_step_edit_select_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """
        Edit the accepted values of an existing single/multi_select field.

        The full comma-separated list is editable, so options can be added,
        removed, renamed, and reordered. Only forward writes are validated
        against the current list: stored records are never rewritten, so a
        value that is removed or renamed simply becomes an orphaned historical
        value that still reads/exports fine. If the field's default value(s)
        are no longer in the list they are pruned, since there is no separate
        default-edit step.
        """
        field = next(f for f in self._fields if f.key == self._editing_field_key)
        errors: dict[str, str] = {}
        raw_options = (
            user_input.get("options", "")
            if user_input is not None
            else ", ".join(field.options or [])
        )
        if user_input is not None:
            items = [o.strip() for o in raw_options.split(",")]
            new_options = [o for o in items if o]
            if not new_options:
                errors["options"] = "options_required"
            elif len(new_options) != len(set(new_options)):
                errors["options"] = "duplicate_option"
            else:
                new_default = _prune_default(field, new_options)
                updated_fields = [
                    FieldDefinition(
                        key=f.key,
                        label=f.label,
                        type=f.type,
                        required=f.required,
                        unit=f.unit,
                        default=new_default if f.key == field.key else f.default,
                        options=new_options if f.key == field.key else f.options,
                        sql_column=f.sql_column,
                    )
                    for f in self._fields
                ]
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    data_updates={"fields": [f.to_dict() for f in updated_fields]},
                )

        return self.async_show_form(
            step_id="edit_select_options",
            data_schema=vol.Schema({vol.Required("options", default=raw_options): str}),
            errors=errors,
            description_placeholders={
                "key": field.key,
                "options": ", ".join(field.options or []),
            },
        )

    async def async_step_set_retention(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Edit retention_days / max_records / warn_at for this record type."""
        record_type = self._current_record_type()
        errors: dict[str, str] = {}
        if user_input is not None:
            values = {
                key: _optional_int(user_input.get(key))
                for key in ("retention_days", "max_records", "warn_at")
            }
            for key, value in values.items():
                if value is not None and value < 1:
                    errors[key] = "positive_integer"
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    data_updates=values,
                )

        return self.async_show_form(
            step_id="set_retention",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "retention_days",
                        description={"suggested_value": record_type.retention_days},
                    ): vol.Any(None, vol.Coerce(int)),
                    vol.Optional(
                        "max_records",
                        description={"suggested_value": record_type.max_records},
                    ): vol.Any(None, vol.Coerce(int)),
                    vol.Optional(
                        "warn_at",
                        description={"suggested_value": record_type.warn_at},
                    ): vol.Any(None, vol.Coerce(int)),
                }
            ),
            errors=errors,
            description_placeholders={"name": record_type.name},
        )

    async def async_step_export_data(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """
        Build a short-lived signed download link for this record type's CSV.

        `include_id` (checked by default) selects "full backup" (id +
        timestamp + fields, safe for idempotent re-import) vs. "data only"
        (drops the internal id, keeps timestamp since it's meaningful data -
        re-importing always creates new records with the original
        timestamps preserved).
        """
        if user_input is not None:
            entry = self._get_entry()
            type_id = _require_str(self._type_id)
            include_id = user_input["include_id"]
            url = (
                f"{EXPORT_URL_PREFIX}/{entry.entry_id}/{type_id}"
                f"?include_id={'true' if include_id else 'false'}"
            )
            signed_url = async_sign_path(self.hass, url, timedelta(minutes=5))
            return self.async_abort(
                reason="export_ready",
                description_placeholders={"download_url": signed_url},
            )

        return self.async_show_form(
            step_id="export_data",
            data_schema=vol.Schema({vol.Required("include_id", default=True): bool}),
        )

    async def async_step_import_data(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Upload a CSV file and import its rows into this record type."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                csv_text = await self.hass.async_add_executor_job(
                    _read_uploaded_csv, self.hass, user_input["file"]
                )
            except ValueError:
                errors["file"] = "file_not_found"
            else:
                record_type = self._current_record_type()
                parse_result = parse_import_csv(record_type, csv_text)
                storage = self._get_entry().runtime_data.storage
                self._import_summary = await storage.async_import_records(
                    record_type.id, parse_result.rows
                )
                self._import_errors = parse_result.errors
                return await self.async_step_import_result()

        return self.async_show_form(
            step_id="import_data",
            data_schema=vol.Schema(
                {
                    vol.Required("file"): selector.FileSelector(
                        selector.FileSelectorConfig(accept=".csv")
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_import_result(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.SubentryFlowResult:
        """Show a one-shot summary of the import that just completed."""
        del user_input
        summary = _require_import_summary(self._import_summary)
        errors_text = "; ".join(
            f"row {error['row']}: {error['message']}"
            for error in self._import_errors[:5]
        )
        return self.async_abort(
            reason="import_complete",
            description_placeholders={
                "imported": str(summary.imported),
                "skipped": str(summary.skipped_duplicate),
                "error_count": str(len(self._import_errors)),
                "errors": errors_text or "(none)",
            },
        )
