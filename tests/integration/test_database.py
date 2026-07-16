from __future__ import annotations

import os
import sqlite3
import stat
from importlib.resources import files
from pathlib import Path

import pytest

from hermes_vocab.database import Database, UnsafeDataDirectoryError


def create_v1_database(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700)
    migration = files("hermes_vocab.migrations").joinpath("001_initial.sql").read_text(
        encoding="utf-8"
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(migration)
        connection.execute(
            """
            INSERT INTO vocabulary_entries (
                id, word, normalized_word, definition, part_of_speech,
                example_sentence, date_added, last_reviewed, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                7,
                "bank",
                "bank",
                "A financial institution.",
                "noun",
                "She visited the bank.",
                "2026-01-01T00:00:00Z",
                "2026-02-01T00:00:00Z",
                "reviewed",
            ),
        )
        connection.execute(
            """
            INSERT INTO review_events (
                id, entry_id, review_date, status, prompted_at,
                answered_at, answer_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                11,
                7,
                "2026-02-01",
                "answered",
                "2026-02-01T08:00:00Z",
                "2026-02-01T08:01:00Z",
                "financial institution",
            ),
        )


def test_fresh_database_applies_v2_schema_and_constraints(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")

    database.initialize()

    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"vocabulary_words", "vocabulary_senses", "review_events"} <= tables
        assert "vocabulary_entries" not in tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO vocabulary_words (
                    word, normalized_word, date_added, review_status
                ) VALUES (?, ?, ?, ?)
                """,
                ("test", "test", "2026-01-01T00:00:00Z", "invalid"),
            )


def test_v1_migration_preserves_words_senses_events_and_answers(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v1_database(path)

    Database(path).initialize()

    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert dict(
            connection.execute(
                "SELECT id, word, normalized_word, date_added, last_reviewed, "
                "review_status FROM vocabulary_words"
            ).fetchone()
        ) == {
            "id": 7,
            "word": "bank",
            "normalized_word": "bank",
            "date_added": "2026-01-01T00:00:00Z",
            "last_reviewed": "2026-02-01T00:00:00Z",
            "review_status": "reviewed",
        }
        assert dict(
            connection.execute(
                "SELECT word_id, definition, part_of_speech, example_sentence, "
                "source_context, date_added FROM vocabulary_senses"
            ).fetchone()
        ) == {
            "word_id": 7,
            "definition": "A financial institution.",
            "part_of_speech": "noun",
            "example_sentence": "She visited the bank.",
            "source_context": None,
            "date_added": "2026-01-01T00:00:00Z",
        }
        assert tuple(
            connection.execute(
                "SELECT id, word_id, review_date, status, prompted_at, "
                "answered_at, answer_text FROM review_events"
            ).fetchone()
        ) == (
            11,
            7,
            "2026-02-01",
            "answered",
            "2026-02-01T08:00:00Z",
            "2026-02-01T08:01:00Z",
            "financial institution",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reopening_migrated_database_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v1_database(path)
    database = Database(path)
    database.initialize()

    database.initialize()

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_words").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 1


def test_v2_foreign_keys_reject_orphans_and_cascade_senses(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()

    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO vocabulary_senses (
                    word_id, definition, part_of_speech, example_sentence, date_added
                ) VALUES (999, 'missing', 'noun', 'Missing.', '2026-01-01T00:00:00Z')
                """
            )
        cursor = connection.execute(
            """
            INSERT INTO vocabulary_words (word, normalized_word, date_added)
            VALUES ('bank', 'bank', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO vocabulary_senses (
                word_id, definition, part_of_speech, example_sentence, date_added
            ) VALUES (?, 'money', 'noun', 'The bank.', '2026-01-01T00:00:00Z')
            """,
            (cursor.lastrowid,),
        )
        connection.execute("DELETE FROM vocabulary_words WHERE id = ?", (cursor.lastrowid,))
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0] == 0


def test_failed_migration_rolls_back_to_intact_v1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v1_database(path)
    original = Database._migration_sql
    broken_v2 = """
    BEGIN IMMEDIATE;
    CREATE TABLE partial_v2 (id INTEGER PRIMARY KEY);
    INSERT INTO missing_table VALUES (1);
    """
    monkeypatch.setattr(
        Database,
        "_migration_sql",
        staticmethod(lambda target: broken_v2 if target == 2 else original(target)),
    )

    with pytest.raises(sqlite3.OperationalError):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entries"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'partial_v2'"
        ).fetchone()[0] == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_database_artifacts_are_private_on_posix(tmp_path: Path) -> None:
    path = tmp_path / "private" / "vocabulary.sqlite3"

    Database(path).initialize()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_existing_world_writable_data_directory_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "unsafe"
    directory.mkdir()
    directory.chmod(0o777)

    with pytest.raises(UnsafeDataDirectoryError):
        Database(directory / "vocabulary.sqlite3").initialize()
