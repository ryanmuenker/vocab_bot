"""Packaged SQLite migrations."""

from collections.abc import Callable
import sqlite3

from .v005_backfill import backfill_v5

MigrationBackfill = Callable[[sqlite3.Connection], None]

MIGRATION_BACKFILLS: dict[int, MigrationBackfill] = {
    5: backfill_v5,
}

__all__ = ["MIGRATION_BACKFILLS", "MigrationBackfill"]
