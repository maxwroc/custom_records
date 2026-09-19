"""Tests for Home Assistant backup coordination."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.custom_records.backup import async_post_backup, async_pre_backup
from custom_components.custom_records.const import DOMAIN

from .conftest import BP_RECORD_TYPE, async_setup_entry_with_types


def _backup_row_count(db_path: Path) -> int:
    """Open the closed database independently, as a backup restore would."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute('SELECT COUNT(*) FROM "records_bp"').fetchone()[0]


async def test_backup_closes_database_and_blocks_writes(
    hass: HomeAssistant,
) -> None:
    """Backup gets a consistent closed database and queued writes resume after it."""
    entry = await async_setup_entry_with_types(hass, [BP_RECORD_TYPE])
    await entry.runtime_data.storage.async_add_record("bp", {"systolic": 120})

    await async_pre_backup(hass)
    blocked_write = asyncio.create_task(
        entry.runtime_data.storage.async_add_record("bp", {"systolic": 130})
    )
    await asyncio.sleep(0)
    assert not blocked_write.done()

    db_path = Path(
        hass.config.path(".storage", DOMAIN, f"custom_records_{entry.entry_id}.db")
    )
    assert await hass.async_add_executor_job(_backup_row_count, db_path) == 1

    await async_post_backup(hass)
    await blocked_write
    assert await entry.runtime_data.storage.async_record_count("bp") == 2
