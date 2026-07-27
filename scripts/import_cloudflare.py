from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from hermes_vocab.cloudflare_snapshot import (
    canonical_bytes,
    extract_snapshot,
    insert_snapshot,
    load_envelope,
    max_ids,
    summary,
    verify_database,
)
from hermes_vocab.database import Database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replace a stopped Hermes vocabulary database")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--replace-stopped-database", action="store_true", required=True)
    return parser.parse_args()


def file_state(paths: list[Path]) -> tuple[tuple[str, int, int] | None, ...]:
    state: list[tuple[str, int, int] | None] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            state.append(None)
        else:
            state.append((path.name, stat.st_size, stat.st_mtime_ns))
    return tuple(state)


def replace_database_files(target: Path, fresh: Path) -> Path | None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = Path(
        tempfile.mkdtemp(prefix=f"{target.name}.backup-{stamp}-", dir=target.parent)
    )
    backup = backup_root / target.name
    moved: list[tuple[Path, Path]] = []
    installed = False
    try:
        for source, destination in (
            (target, backup),
            (Path(f"{target}-wal"), Path(f"{backup}-wal")),
            (Path(f"{target}-shm"), Path(f"{backup}-shm")),
        ):
            if source.exists():
                source.rename(destination)
                moved.append((source, destination))
        fresh.rename(target)
        installed = True
        target.chmod(0o600)
    except BaseException:
        try:
            if installed:
                target.unlink(missing_ok=True)
            for source, destination in reversed(moved):
                if destination.exists():
                    destination.rename(source)
            if not any(backup_root.iterdir()):
                backup_root.rmdir()
        except BaseException as recovery_error:
            raise RuntimeError("failed to restore original database after replacement failure") from recovery_error
        raise
    if not moved:
        backup_root.rmdir()
        return None
    return backup_root


def main() -> None:
    args = parse_args()
    target = args.database.expanduser().resolve()
    envelope = load_envelope(args.input.expanduser().resolve())
    snapshot = envelope["snapshot"]
    digest = envelope["sha256"]

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh_fd, fresh_name = tempfile.mkstemp(
        prefix=f".{target.name}.cloudflare-import-",
        dir=target.parent,
    )
    os.close(fresh_fd)
    fresh = Path(fresh_name)
    fresh.unlink()
    try:
        Database(fresh).initialize()
        connection = sqlite3.connect(fresh)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            insert_snapshot(connection, snapshot)
            connection.commit()
            verify_database(connection)
            restored = extract_snapshot(connection)
            if canonical_bytes(restored) != canonical_bytes(snapshot):
                raise ValueError("restored database snapshot differs")
            if max_ids(restored) != max_ids(snapshot):
                raise ValueError("restored database max IDs differ")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            connection.execute("PRAGMA journal_mode = DELETE").fetchall()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        fresh.chmod(0o600)

        watched = [target, Path(f"{target}-wal"), Path(f"{target}-shm")]
        before = file_state(watched)
        time.sleep(0.1)
        if file_state(watched) != before:
            raise RuntimeError("stopped database WAL/SHM files are changing")

        replace_database_files(target, fresh)
        print(json.dumps(summary(snapshot, digest), sort_keys=True, separators=(",", ":")))
    finally:
        fresh.unlink(missing_ok=True)
        Path(f"{fresh}-wal").unlink(missing_ok=True)
        Path(f"{fresh}-shm").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
