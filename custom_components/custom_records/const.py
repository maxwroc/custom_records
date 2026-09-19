"""Constants for custom_records."""

from __future__ import annotations

import re
from enum import StrEnum
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "custom_records"
STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = f"{DOMAIN}_{{entry_id}}_{{record_type_id}}"

# Delay (seconds) used for the debounced/coalesced Store save pattern.
SAVE_DELAY = 10

# Services
SERVICE_ADD_RECORD = "add_record"
SERVICE_EXPORT_RECORDS = "export_records"
SERVICE_IMPORT_RECORDS = "import_records"

# Service/record field names (public shape)
ATTR_RECORD_TYPE = "record_type"
ATTR_FIELDS = "fields"
ATTR_TIMESTAMP = "timestamp"
ATTR_RECORD_ID = "id"

# Public shape of an IMAGE-type field value, e.g.
# {"media_source": "media-source://custom_records/<type>/<id>/<field>"}.
# Generated at serialization time (record_view.py); never stored.
ATTR_MEDIA_SOURCE = "media_source"

# custom_records/list_records WebSocket command: optional page-size param, and
# a hard server-side cap applied regardless of what a caller (e.g. the card)
# requests, to keep response payload size bounded as a record type grows.
ATTR_LIMIT = "limit"
MAX_LIST_RECORDS_LIMIT = 500

# custom_records/list_records WebSocket command: optional server-side row
# filter (P0-9) - a list of single-key {field_key: value} maps, AND-combined.
# See filter_query.py for the compiled SQL WHERE fragment this is turned into.
ATTR_FILTER = "filter"

# custom_records/aggregate_records WebSocket command field names.
ATTR_OP = "op"
ATTR_BUCKET = "bucket"
ATTR_FIELD = "field"
ATTR_FORMAT = "format"
ATTR_START = "start"
ATTR_END = "end"


# CSV export/import (custom_records.export_records/import_records services and
# the record_type subentry's "Export data"/"Import data" reconfigure steps).
ATTR_INCLUDE_ID = "include_id"
ATTR_PATH = "path"
ATTR_CONTENT = "content"

# URL prefix under which per-record-type CSV exports are served (mirrors
# MEDIA_URL_PREFIX in media_store.py) - a real HomeAssistantView
# (requires_auth=True), accessible either with a Bearer token or a
# short-lived signed-URL query param (see config_flow.py's export_data step).
EXPORT_URL_PREFIX = f"/{DOMAIN}_export"

# Reserved/compact keys used in the on-disk record envelope. These are internal
# only and never exposed to the user for renaming - kept short since they
# appear on every single stored record.
ENVELOPE_ID = "id"
ENVELOPE_TIMESTAMP = "t"
ENVELOPE_DATA = "d"

# Reserved words that cannot be used as user-defined field keys, since the
# public API flattens the envelope + field data into a single dict and a
# field named one of these would silently shadow the envelope's own value.
RESERVED_FIELD_KEYS = {"id", "timestamp", "record_type"}

# Options flow storage keys (config entry options) - legacy, pre-subentries
# storage format. Still read once at setup for a one-time migration into
# ConfigSubentry-based storage (see __init__.py); no longer written to.
CONF_RECORD_TYPES = "record_types"

# Each configured record type is stored as a ConfigSubentry of this type, so
# it appears as a visible, individually manageable sub-item directly on the
# integration's card in Settings > Devices & Services.
SUBENTRY_TYPE_RECORD_TYPE = "record_type"

RECORD_TYPE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def is_valid_record_type_id(value: str) -> bool:
    """Return whether value is safe for storage keys, URLs, and media paths."""
    return RECORD_TYPE_ID_PATTERN.fullmatch(value) is not None


# Fired on hass.bus whenever a record type's data OR its own definition
# changes (add/delete/purge/max_records-eviction, or a reload triggered by a
# config subentry add/update/remove) - lets already-open Lovelace cards
# refetch instead of silently going stale. Payload is deliberately minimal
# (ids only, never field values) since bus events are broadcast to every
# authenticated listener and record data can be personal/sensitive.
EVENT_RECORDS_UPDATED = f"{DOMAIN}_updated"
ATTR_ENTRY_ID = "entry_id"


class FieldType(StrEnum):
    """Supported record field data types."""

    NUMBER = "number"
    TEXT = "text"
    LONG_TEXT = "long_text"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    IMAGE = "image"


# Field types that require a user-defined list of options.
SELECT_FIELD_TYPES = {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}

# --- SQLite storage (plan_sql.md) --------------------------------------

# Same restricted pattern is reused for field keys - both become SQL
# identifiers (record-type id -> table name, field key -> column name).
FIELD_KEY_PATTERN = RECORD_TYPE_ID_PATTERN


def is_valid_field_key(value: str) -> bool:
    """Return whether value is safe as a SQL column name."""
    return FIELD_KEY_PATTERN.fullmatch(value) is not None


# Conservative bound on generated/validated SQL identifier length (well under
# any SQLite limit; keeps table/column names readable in PRAGMA output).
MAX_IDENTIFIER_LENGTH = 63

# A representative (not exhaustive - identifiers are always double-quoted
# anyway) set of SQL keywords disallowed as field keys, so a field never
# produces a confusing/ambiguous column name in ad-hoc SQL/PRAGMA output.
RESERVED_SQL_KEYWORDS = {
    "select",
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "table",
    "index",
    "trigger",
    "view",
    "from",
    "where",
    "join",
    "union",
    "group",
    "order",
    "by",
    "having",
    "limit",
    "offset",
    "and",
    "or",
    "not",
    "null",
    "true",
    "false",
    "primary",
    "key",
    "foreign",
    "references",
    "check",
    "unique",
    "default",
    "values",
    "into",
    "set",
    "as",
    "on",
    "pragma",
    "attach",
    "detach",
    "transaction",
    "commit",
    "rollback",
}

# Physical table name for a record type: "records_<record_type_id>".
SQL_TABLE_PREFIX = "records_"

# Physical database file, one per config entry (single_config_entry is true,
# so in practice there is exactly one), under HA's backed-up .storage dir.
DB_FILENAME_TEMPLATE = f"{DOMAIN}_{{entry_id}}.db"

# Tracks the on-disk schema format via `PRAGMA user_version`; bump and add an
# explicit migration step whenever the physical schema contract changes.
DB_SCHEMA_VERSION = 1

# Fixed, non-configurable base columns present on every record table.
COL_ID = "id"
COL_TIMESTAMP = "timestamp"

# Physical SQL storage type for each logical field type.
SQL_TYPE_FOR_FIELD_TYPE: dict[FieldType, str] = {
    FieldType.NUMBER: "REAL",
    FieldType.BOOLEAN: "INTEGER",
    FieldType.DATETIME: "INTEGER",
    FieldType.TEXT: "TEXT",
    FieldType.LONG_TEXT: "TEXT",
    FieldType.SINGLE_SELECT: "TEXT",
    FieldType.MULTI_SELECT: "TEXT",
    FieldType.IMAGE: "TEXT",
}


class AggregateOp(StrEnum):
    """Supported `custom_records/aggregate_records` operations."""

    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"


# Operations that aggregate a required numeric `field` (as opposed to COUNT,
# which counts records and forbids `field`).
NUMERIC_AGGREGATE_OPS = {
    AggregateOp.SUM,
    AggregateOp.AVG,
    AggregateOp.MIN,
    AggregateOp.MAX,
}


class AggregateBucket(StrEnum):
    """Supported `custom_records/aggregate_records` calendar bucket sizes."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class AggregateFormat(StrEnum):
    """Supported `custom_records/aggregate_records` response shapes."""

    TABLE = "table"
    APEXCHARTS = "apexcharts"


# custom_records/aggregate_records WebSocket command: v2 extensions
# (group_by, multi-metric, cumulative - see plan_sql.md follow-up plan).
ATTR_GROUP_BY = "group_by"
ATTR_METRICS = "metrics"
ATTR_METRIC_NAME = "name"
ATTR_CUMULATIVE = "cumulative"

# Bounds a single aggregate_records call's `metrics` list length, so a
# pathological request can't force one query to compute an unbounded number
# of aggregate expressions.
MAX_METRICS_PER_CALL = 10

# Custom fixed-size bucket duration string, e.g. "15m"/"2h" - restricted to
# minutes/hours only. Day/week/month+ granularity must stay calendar-aware
# via the named AggregateBucket values (a fixed N-day/week epoch-aligned
# bucket would not agree with AggregateBucket.WEEK's Monday-aligned
# semantics, creating a second, confusing "week" concept).
CUSTOM_BUCKET_PATTERN = re.compile(r"^(\d+)(m|h)$", re.IGNORECASE)

# `bucket: "auto"` thresholds (inclusive span in days) for picking a
# named AggregateBucket from a caller-supplied start/end range.
AUTO_BUCKET_HOUR_MAX_DAYS = 2
AUTO_BUCKET_DAY_MAX_DAYS = 90
AUTO_BUCKET_WEEK_MAX_DAYS = 730

# custom_records/get_field_stats WebSocket command.
ATTR_STATS = "stats"
ALL_FIELD_STATS = ("first", "last", "min", "max", "sum", "avg", "count")

# custom_records/histogram_records WebSocket command.
ATTR_BIN_COUNT = "bin_count"
ATTR_BIN_WIDTH = "bin_width"
ATTR_MIN = "min"
ATTR_MAX = "max"
DEFAULT_HISTOGRAM_BIN_COUNT = 10
MAX_HISTOGRAM_BINS = 100

# custom_records/compare_periods WebSocket command.
ATTR_CURRENT = "current"
ATTR_PREVIOUS = "previous"
