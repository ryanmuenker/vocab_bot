from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
import sqlite3
import stat
from importlib.resources import files
from pathlib import Path
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from hermes_vocab.database import Database, UnsafeDataDirectoryError
from hermes_vocab.models import (
    Evaluation,
    EvaluationGrade,
    ReviewCompletionStatus,
    ReviewPromptStatus,
    TestCompletionStatus as VocabularyTestCompletionStatus,
    TestStartStatus as VocabularyTestStartStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService as VocabularyTestSessionService


def _migration(name: str) -> str:
    return files("hermes_vocab.migrations").joinpath(name).read_text(encoding="utf-8")


def create_v1_database(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(path) as connection:
        connection.executescript(_migration("001_initial.sql"))
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


def create_v2_database(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(path) as connection:
        connection.executescript(_migration("001_initial.sql"))
        connection.executescript(_migration("002_multi_sense.sql"))
        connection.execute(
            """
            INSERT INTO vocabulary_words (
                id, word, normalized_word, date_added, last_reviewed, review_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                7,
                "Pro Forma",
                "pro forma",
                "2026-01-01T00:00:00Z",
                "2026-02-01T00:00:00Z",
                "reviewed",
            ),
        )
        connection.executemany(
            """
            INSERT INTO vocabulary_senses (
                id, word_id, definition, part_of_speech, example_sentence,
                source_context, date_added
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    21,
                    7,
                    "As a matter of form.",
                    "adjective",
                    "The board issued a pro forma approval.",
                    "The approval was pro forma.",
                    "2026-01-01T00:00:00Z",
                ),
                (
                    22,
                    7,
                    "A projected financial statement.",
                    "noun",
                    "The lender reviewed the pro forma.",
                    "The pro forma showed next year's revenue.",
                    "2026-01-02T00:00:00Z",
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO review_events (
                id, word_id, review_date, status, prompted_at,
                answered_at, answer_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                31,
                7,
                "2026-02-01",
                "answered",
                "2026-02-01T08:00:00Z",
                "2026-02-01T08:01:00Z",
                "done for form's sake",
            ),
        )


def create_v3_database(path: Path) -> None:
    create_v2_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(_migration("003_entry_terms.sql"))


def create_populated_v3_database(path: Path) -> None:
    create_v3_database(path)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO vocabulary_entries (
                id, display_text, normalized_text, date_added,
                last_reviewed, review_status
            ) VALUES (?, ?, ?, ?, NULL, 'new')
            """,
            (
                (
                    entry_id,
                    f"word-{position}",
                    f"word-{position}",
                    f"2026-01-{position + 1:02d}T00:00:00Z",
                )
                for position, entry_id in enumerate(range(8, 12), start=1)
            ),
        )
        connection.executemany(
            """
            INSERT INTO vocabulary_senses (
                id, entry_id, definition, part_of_speech,
                example_sentence, source_context, date_added
            ) VALUES (?, ?, ?, 'noun', ?, NULL, ?)
            """,
            (
                (
                    20 + position,
                    entry_id,
                    f"Definition {position}.",
                    f"Example {position}.",
                    f"2026-01-{position + 1:02d}T00:00:00Z",
                )
                for position, entry_id in enumerate(range(8, 12), start=3)
            ),
        )


def initialize_concurrently(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synchronized_target: int,
) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    barrier = Barrier(2)
    original = Database._apply_migration

    def synchronized_apply(
        database: Database,
        connection: sqlite3.Connection,
        target: int,
    ) -> None:
        if target == synchronized_target:
            barrier.wait(timeout=5)
        original(database, connection, target)

    monkeypatch.setattr(Database, "_apply_migration", synchronized_apply)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(Database(path).initialize) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)


def test_concurrent_fresh_initialization_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"

    initialize_concurrently(path, monkeypatch, synchronized_target=1)

    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"vocabulary_entries", "vocabulary_senses", "review_events"} <= tables
        assert "vocabulary_words" not in tables


def test_concurrent_v2_initialization_preserves_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v2_database(path)

    initialize_concurrently(path, monkeypatch, synchronized_target=3)

    _assert_v2_fixture_migrated(path)


def test_concurrent_v3_initialization_converges_on_intact_v4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v3_database(path)

    initialize_concurrently(path, monkeypatch, synchronized_target=4)

    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entries"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM review_events"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM test_sessions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM test_questions"
        ).fetchone()[0] == 0


def test_migration_rejects_skipping_a_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v1_database(path)
    database = Database(path)

    with database.connect() as connection:
        with pytest.raises(
            RuntimeError,
            match="Cannot apply database migration 3 from schema version 1",
        ):
            database._apply_migration(connection, 3)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_fresh_database_applies_v4_schema_and_constraints(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")

    database.initialize()

    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "vocabulary_entries",
            "vocabulary_senses",
            "review_events",
            "test_sessions",
            "test_questions",
        } <= tables
        assert "vocabulary_words" not in tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO vocabulary_entries (
                    display_text, normalized_text, date_added, review_status
                ) VALUES (?, ?, ?, ?)
                """,
                ("test", "test", "2026-01-01T00:00:00Z", "invalid"),
            )


def test_v1_migration_preserves_entry_sense_event_and_answer(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v1_database(path)

    Database(path).initialize()

    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert dict(
            connection.execute(
                "SELECT id, display_text, normalized_text, date_added, last_reviewed, "
                "review_status FROM vocabulary_entries"
            ).fetchone()
        ) == {
            "id": 7,
            "display_text": "bank",
            "normalized_text": "bank",
            "date_added": "2026-01-01T00:00:00Z",
            "last_reviewed": "2026-02-01T00:00:00Z",
            "review_status": "reviewed",
        }
        assert dict(
            connection.execute(
                "SELECT entry_id, definition, part_of_speech, example_sentence, "
                "source_context, date_added FROM vocabulary_senses"
            ).fetchone()
        ) == {
            "entry_id": 7,
            "definition": "A financial institution.",
            "part_of_speech": "noun",
            "example_sentence": "She visited the bank.",
            "source_context": None,
            "date_added": "2026-01-01T00:00:00Z",
        }
        assert tuple(
            connection.execute(
                "SELECT id, entry_id, review_date, status, prompted_at, "
                "answered_at, answer_text, grade, evaluation_feedback "
                "FROM review_events"
            ).fetchone()
        ) == (
            11,
            7,
            "2026-02-01",
            "answered",
            "2026-02-01T08:00:00Z",
            "2026-02-01T08:01:00Z",
            "financial institution",
            None,
            None,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v2_migration_preserves_entries_senses_and_reviews(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v2_database(path)

    Database(path).initialize()

    _assert_v2_fixture_migrated(path)


def _assert_v2_fixture_migrated(path: Path) -> None:
    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"vocabulary_entries", "vocabulary_senses", "review_events"} <= tables
        assert "vocabulary_words" not in tables
        assert tuple(
            connection.execute(
                """
                SELECT id, display_text, normalized_text,
                       date_added, last_reviewed, review_status
                FROM vocabulary_entries
                """
            ).fetchone()
        ) == (
            7,
            "Pro Forma",
            "pro forma",
            "2026-01-01T00:00:00Z",
            "2026-02-01T00:00:00Z",
            "reviewed",
        )
        assert [tuple(row) for row in connection.execute(
            """
            SELECT id, entry_id, definition, part_of_speech, example_sentence,
                   source_context, date_added
            FROM vocabulary_senses ORDER BY id
            """
        )] == [
            (
                21,
                7,
                "As a matter of form.",
                "adjective",
                "The board issued a pro forma approval.",
                "The approval was pro forma.",
                "2026-01-01T00:00:00Z",
            ),
            (
                22,
                7,
                "A projected financial statement.",
                "noun",
                "The lender reviewed the pro forma.",
                "The pro forma showed next year's revenue.",
                "2026-01-02T00:00:00Z",
            ),
        ]
        assert tuple(connection.execute(
            """
            SELECT id, entry_id, review_date, status, prompted_at,
                   answered_at, answer_text
            FROM review_events
            """
        ).fetchone()) == (
            31,
            7,
            "2026-02-01",
            "answered",
            "2026-02-01T08:00:00Z",
            "2026-02-01T08:01:00Z",
            "done for form's sake",
        )


def test_reopening_migrated_database_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v2_database(path)
    database = Database(path)
    database.initialize()

    database.initialize()

    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_entries").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 1


def test_v3_foreign_keys_reject_orphans_and_cascade_senses(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()

    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO vocabulary_senses (
                    entry_id, definition, part_of_speech, example_sentence, date_added
                ) VALUES (999, 'missing', 'noun', 'Missing.', '2026-01-01T00:00:00Z')
                """
            )
        cursor = connection.execute(
            """
            INSERT INTO vocabulary_entries (display_text, normalized_text, date_added)
            VALUES ('bank', 'bank', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO vocabulary_senses (
                entry_id, definition, part_of_speech, example_sentence, date_added
            ) VALUES (?, 'money', 'noun', 'The bank.', '2026-01-01T00:00:00Z')
            """,
            (cursor.lastrowid,),
        )
        connection.execute(
            "DELETE FROM vocabulary_entries WHERE id = ?", (cursor.lastrowid,)
        )
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0] == 0


def test_failed_initial_migration_leaves_empty_database_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    original = Database._migration_sql
    broken_v1 = """
    CREATE TABLE partial_v1 (id INTEGER PRIMARY KEY);
    INSERT INTO missing_table VALUES (1);
    PRAGMA user_version = 1;
    """
    monkeypatch.setattr(
        Database,
        "_migration_sql",
        staticmethod(lambda target: broken_v1 if target == 1 else original(target)),
    )

    with pytest.raises(sqlite3.OperationalError):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'partial_v1'"
        ).fetchone()[0] == 0

    monkeypatch.setattr(Database, "_migration_sql", staticmethod(original))
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'vocabulary_entries'"
        ).fetchone()[0] == 1


def test_failed_v3_migration_rolls_back_to_intact_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v2_database(path)
    original = Database._migration_sql
    broken_v3 = """
    ALTER TABLE vocabulary_words RENAME TO vocabulary_entries;
    INSERT INTO missing_table VALUES (1);
    """
    monkeypatch.setattr(
        Database,
        "_migration_sql",
        staticmethod(lambda target: broken_v3 if target == 3 else original(target)),
    )

    with pytest.raises(sqlite3.OperationalError):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_words").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'vocabulary_entries'"
        ).fetchone()[0] == 0


def test_failed_v4_migration_rolls_back_to_intact_populated_v3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v3_database(path)
    original = Database._migration_sql
    broken_v4 = """
    ALTER TABLE review_events ADD COLUMN grade TEXT;
    CREATE TABLE partial_test_sessions (id INTEGER PRIMARY KEY);
    INSERT INTO missing_table VALUES (1);
    """
    monkeypatch.setattr(
        Database,
        "_migration_sql",
        staticmethod(lambda target: broken_v4 if target == 4 else original(target)),
    )

    with pytest.raises(sqlite3.OperationalError):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entries"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT answer_text FROM review_events"
        ).fetchone()[0] == "done for form's sake"
        review_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(review_events)")
        }
        assert "grade" not in review_columns
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name IN "
            "('partial_test_sessions', 'test_sessions', 'test_questions')"
        ).fetchone()[0] == 0


def test_v4_constraints_reject_invalid_grades_statuses_and_question_identity(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    with database.connect() as connection:
        entry_ids = []
        for index in range(2):
            cursor = connection.execute(
                """
                INSERT INTO vocabulary_entries (
                    display_text, normalized_text, date_added
                ) VALUES (?, ?, '2026-01-01T00:00:00Z')
                """,
                (f"word-{index}", f"word-{index}"),
            )
            entry_ids.append(cursor.lastrowid)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO test_sessions (status, started_at)
                VALUES ('invalid', '2026-01-01T00:00:00Z')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO test_sessions (status, started_at, completed_at)
                VALUES (
                    'active',
                    '2026-01-01T00:00:00Z',
                    '2026-01-01T00:01:00Z'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO test_sessions (status, started_at, completed_at)
                VALUES ('completed', '2026-01-01T00:00:00Z', NULL)
                """
            )
        session_id = connection.execute(
            """
            INSERT INTO test_sessions (status, started_at)
            VALUES ('active', '2026-01-01T00:00:00Z')
            """
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO test_sessions (status, started_at)
                VALUES ('active', '2026-01-02T00:00:00Z')
                """
            )
        for invalid_position in (0, 6):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO test_questions (session_id, entry_id, position)
                    VALUES (?, ?, ?)
                    """,
                    (session_id, entry_ids[0], invalid_position),
                )
        question_id = connection.execute(
            """
            INSERT INTO test_questions (session_id, entry_id, position)
            VALUES (?, ?, 1)
            """,
            (session_id, entry_ids[0]),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO test_questions (session_id, entry_id, position)
                VALUES (?, ?, 1)
                """,
                (session_id, entry_ids[1]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO test_questions (session_id, entry_id, position)
                VALUES (?, ?, 2)
                """,
                (session_id, entry_ids[0]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE test_questions
                SET answer_text = 'raw', grade = 'invalid',
                    evaluation_feedback = 'feedback',
                    answered_at = '2026-01-01T00:01:00Z'
                WHERE id = ?
                """,
                (question_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE test_questions
                SET answer_text = 'raw', grade = 'correct'
                WHERE id = ?
                """,
                (question_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO review_events (
                    entry_id, review_date, status, prompted_at, grade
                ) VALUES (?, '2026-01-01', 'pending',
                          '2026-01-01T00:00:00Z', 'invalid')
                """,
                (entry_ids[0],),
            )

def test_populated_v3_upgrade_preserves_review_history_and_test_scheduling(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_populated_v3_database(path)
    database = Database(path)
    database.initialize()
    timezone = ZoneInfo("UTC")

    review = ReviewService(
        database,
        timezone,
        clock=lambda: datetime(2026, 7, 19, 8, tzinfo=UTC),
    )
    prompt = review.daily_review()
    assert prompt.status is ReviewPromptStatus.PENDING
    assert prompt.event is not None
    assert prompt.entry is not None
    assert prompt.entry.display_text == "word-1"
    completion = review.complete_review(
        prompt.event.id,
        "A partially remembered definition.",
        Evaluation(EvaluationGrade.PARTIAL, "The central detail was missing."),
    )
    assert completion.status is ReviewCompletionStatus.COMPLETED

    with database.connect() as connection:
        scheduling_before_test = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, last_reviewed, review_status
                FROM vocabulary_entries
                ORDER BY id
                """
            )
        ]

    tests = VocabularyTestSessionService(
        database,
        clock=lambda: datetime(2026, 7, 19, 9, tzinfo=UTC),
    )
    started = tests.start()
    assert started.status is VocabularyTestStartStatus.STARTED
    assert started.snapshot is not None
    grades = (
        EvaluationGrade.CORRECT,
        EvaluationGrade.PARTIAL,
        EvaluationGrade.INCORRECT,
        EvaluationGrade.CORRECT,
        EvaluationGrade.PARTIAL,
    )
    result = None
    for question, grade in zip(started.snapshot.questions, grades, strict=True):
        result = tests.complete(
            question.id,
            f"answer {question.position}",
            Evaluation(grade, f"Feedback {question.position}."),
        )
    assert result is not None
    assert result.status is VocabularyTestCompletionStatus.COMPLETED
    assert result.snapshot is not None
    assert (
        result.snapshot.summary.correct,
        result.snapshot.summary.partial,
        result.snapshot.summary.incorrect,
    ) == (2, 2, 1)

    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, review_date, status, answer_text, grade,
                       evaluation_feedback
                FROM review_events
                ORDER BY id
                """
            )
        ] == [
            (
                31,
                "2026-02-01",
                "answered",
                "done for form's sake",
                None,
                None,
            ),
            (
                prompt.event.id,
                "2026-07-19",
                "answered",
                "A partially remembered definition.",
                "partial",
                "The central detail was missing.",
            ),
        ]
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, last_reviewed, review_status
                FROM vocabulary_entries
                ORDER BY id
                """
            )
        ] == scheduling_before_test


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
