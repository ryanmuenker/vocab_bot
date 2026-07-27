from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from hermes_vocab.cloudflare_snapshot import extract_snapshot, snapshot_sha256, summary, verify_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a Hermes vocabulary v5 snapshot")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database = args.database.expanduser().resolve()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_fd, backup_name = tempfile.mkstemp(prefix="hermes-vocab-backup-", suffix=".sqlite3")
    os.close(backup_fd)
    backup = Path(backup_name)
    backup.chmod(0o600)
    try:
        source = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
        try:
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        connection = sqlite3.connect(backup)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            verify_database(connection)
            snapshot = extract_snapshot(connection)
        finally:
            connection.close()
        digest = snapshot_sha256(snapshot)
        envelope = {"sha256": digest, "snapshot": snapshot}
        output_fd, output_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        try:
            with os.fdopen(output_fd, "w", encoding="utf-8") as handle:
                json.dump(
                    envelope,
                    handle,
                    ensure_ascii=False,
                    sort_keys=args.pretty,
                    indent=2 if args.pretty else None,
                    separators=None if args.pretty else (",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(output_name).chmod(0o600)
            os.replace(output_name, output)
        finally:
            Path(output_name).unlink(missing_ok=True)
        print(json.dumps(summary(snapshot, digest), sort_keys=True, separators=(",", ":")))
    finally:
        backup.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
