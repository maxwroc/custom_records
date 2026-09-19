"""Fixtures and shared helpers for custom_records tests."""

from __future__ import annotations

import shutil
from collections.abc import Generator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.custom_records.const import DOMAIN, SUBENTRY_TYPE_RECORD_TYPE

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading this custom integration for every test in this suite."""
    del enable_custom_integrations
    yield


@pytest.fixture(autouse=True)
def _cleanup_custom_records_storage(hass: HomeAssistant) -> Generator[None]:
    """
    Remove custom_records's on-disk SQLite database directory around each test.

    Unlike `homeassistant.helpers.storage.Store` (transparently virtualized
    in-memory by PHACC's own `hass_storage` fixture), store.py's
    `RecordStorage` does REAL on-disk SQLite I/O, and PHACC's `hass` fixture
    shares one persistent testing_config dir across tests/runs (see
    make_source_image's docstring below) - without this, a fixed entry_id
    like "entry1" reused across many tests would keep accumulating rows in
    the SAME physical database file forever.
    """
    storage_dir = Path(hass.config.path(".storage", DOMAIN))
    shutil.rmtree(storage_dir, ignore_errors=True)
    yield
    shutil.rmtree(storage_dir, ignore_errors=True)


def make_source_image(hass: HomeAssistant, name: str = "photo.jpg") -> Path:
    """
    Create a fake source image file inside an allowed root for IMAGE fields.

    IMAGE field source paths must resolve inside the HA config dir (see
    media_store.allowed_source_roots), so test fixtures write under
    hass.config.path() rather than pytest's tmp_path. Each call uses a fresh,
    randomly-named subdirectory since PHACC's hass fixture shares one
    persistent testing_config dir across tests/runs (see repo memory notes on
    test isolation).
    """
    source_dir = Path(hass.config.path("test_media_sources", uuid4().hex))
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / name
    source.write_bytes(b"fake-image-bytes")
    return source


def make_csv_source(
    hass: HomeAssistant, csv_text: str, name: str = "import.csv"
) -> Path:
    """
    Write csv_text to a fresh file inside an allowed root, for `path`-based tests.

    Same rationale/isolation approach as make_source_image (allow-listed-root
    + per-call random subdirectory) - see its docstring. A plain (non-async)
    helper deliberately, so its blocking Path I/O isn't flagged (ASYNC240) or
    actually run on the event loop, when called from async test functions.
    """
    source_dir = Path(hass.config.path("test_csv_sources", uuid4().hex))
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / name
    source.write_text(csv_text, encoding="utf-8")
    return source


def read_text_file(path: str) -> str:
    """Read a text file synchronously - see make_csv_source's ASYNC240 note."""
    return Path(path).read_text(encoding="utf-8")


# A minimal "Blood Pressure" record type used across multiple test modules.
BP_RECORD_TYPE: dict[str, Any] = {
    "id": "bp",
    "name": "Blood Pressure",
    "fields": [
        {
            "key": "systolic",
            "label": "Systolic",
            "type": "number",
            "required": True,
            "unit": None,
            "default": None,
            "options": None,
        },
    ],
    "timestamp_field": "timestamp",
    "retention_days": None,
    "max_records": None,
    "warn_at": None,
}


async def async_setup_entry_with_types(
    hass: HomeAssistant, record_types: list[dict[str, Any]] | None = None
) -> MockConfigEntry:
    """Create and set up a config entry, optionally pre-loaded with record types."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        subentries_data=[
            {
                "data": {k: v for k, v in rt.items() if k not in ("id", "name")},
                "subentry_type": SUBENTRY_TYPE_RECORD_TYPE,
                "title": rt["name"],
                "unique_id": rt["id"],
            }
            for rt in (record_types or [])
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
