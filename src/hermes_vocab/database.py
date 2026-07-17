from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator

_MIGRATIONS = {
    1: "001_initial.sql",
    2: "002_multi_sense.sql",
}


class UnsafeDataDirectoryError(RuntimeError):
    """Raised when the data directory is accessible to other users."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def initialize(self) -> None:
        self._prepare_directory()
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            latest = max(_MIGRATIONS)
            if version > latest:
                raise RuntimeError(
                    f"Unsupported database schema version: {version}"
                )
            for target in range(version + 1, latest + 1):
                self._apply_migration(connection, target)
        self._tighten_file_mode(self.path)
        for suffix in ("-wal", "-shm"):
            self._tighten_file_mode(Path(f"{self.path}{suffix}"))

    @staticmethod
    def _migration_sql(target: int) -> str:
        return (
            files("hermes_vocab.migrations")
            .joinpath(_MIGRATIONS[target])
            .read_text(encoding="utf-8")
        )

    @staticmethod
    def _migration_statements(script: str) -> Iterator[str]:
        buffer: list[str] = []
        for character in script:
            buffer.append(character)
            if character == ";":
                statement = "".join(buffer)
                if sqlite3.complete_statement(statement):
                    yield statement
                    buffer.clear()
        trailing = "".join(buffer).strip()
        if trailing:
            yield trailing

    def _apply_migration(
        self,
        connection: sqlite3.Connection,
        target: int,
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < target:
                if version != target - 1:
                    raise RuntimeError(
                        f"Cannot apply database migration {target} "
                        f"from schema version {version}"
                    )
                for statement in self._migration_statements(
                    self._migration_sql(target)
                ):
                    connection.execute(statement)
                violations = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        f"Foreign-key violations: {violations}"
                    )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _prepare_directory(self) -> None:
        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o700)
        if os.name != "posix":
            return
        info = directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise UnsafeDataDirectoryError(
                f"Data directory must be owned by the current user and mode 0700: {directory}"
            )
        directory.chmod(0o700)

    @staticmethod
    def _tighten_file_mode(path: Path) -> None:
        if os.name == "posix" and path.exists():
            path.chmod(0o600)
