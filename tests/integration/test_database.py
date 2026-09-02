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
    CardDirection,
    DeliveryAttemptStatus,
    Evaluation,
    EvaluationGrade,
    FinalizeStatus,
    ReviewRating,
    StudyMutationStatus,
    StudyMode,
    StudyPromptStatus,
    StudyQueueStatus,
    StudySessionStatus,
    StudyStartStatus,
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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


def test_fresh_database_applies_v6_schema_and_constraints(tmp_path: Path) -> None:
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
            "vocabulary_entry_images",
            "image_backfill_attempts",
        } <= tables
        assert "vocabulary_words" not in tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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

def test_populated_v3_upgrade_preserves_legacy_audit_and_uses_v5_scheduling(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_populated_v3_database(path)
    database = Database(path)
    database.initialize()
    timezone = ZoneInfo("UTC")
    with database.connect() as connection:
        legacy_events_before = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, review_date, status, answer_text, grade,
                       evaluation_feedback
                FROM review_events
                ORDER BY id
                """
            )
        ]
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

    review = ReviewService(
        database,
        timezone,
        clock=lambda: datetime(2026, 7, 19, 8, tzinfo=UTC),
    )
    started_review = review.start()
    assert started_review.status is StudyStartStatus.STARTED
    assert started_review.snapshot is not None
    assert len(started_review.snapshot.queue) == 5
    current = next(
        item
        for item in started_review.snapshot.queue
        if item.status is StudyQueueStatus.CURRENT
    )
    prompt = review.prepare_current_prompt(
        "v3-upgrade-review-1",
        "Review the migrated vocabulary card.",
    )
    assert prompt is not None
    delivered = review.record_delivery(
        prompt.id,
        delivery_id="v3-upgrade-delivery-1",
        content_fingerprint="v3-upgrade-fingerprint-1",
    )
    assert delivered is not None
    assert review.record_answer(
        prompt.id,
        "A partially remembered definition.",
        Evaluation(EvaluationGrade.PARTIAL, "The central detail was missing."),
    ) is not None
    completion = review.finalize(prompt.id, ReviewRating.HARD)
    assert completion.status is FinalizeStatus.COMPLETED
    assert completion.transition is not None
    assert completion.transition.before.state.value == "new"
    assert completion.transition.after.state.value == "review"
    assert review.exit() is StudyMutationStatus.COMPLETED
    # Review selection introduced all five distinct entries, so no unseen
    # forward entry remains eligible for an explicit introduction test.
    tests = VocabularyTestSessionService(
        database,
        timezone,
        clock=lambda: datetime(2026, 7, 19, 9, tzinfo=UTC),
    )
    started = tests.start(CardDirection.FORWARD)
    assert started.status is StudyStartStatus.EMPTY
    assert started.available_count == 0
    assert started.snapshot is None

    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        ] == legacy_events_before
        assert tuple(
            connection.execute(
                """
                SELECT card_id, source, rating, before_state, after_state,
                       before_repetitions, after_repetitions
                FROM review_attempts
                WHERE prompt_id = ?
                """,
                (prompt.id,),
            ).fetchone()
        ) == (
            current.card.id,
            "review",
            "hard",
            "new",
            "review",
            0,
            1,
        )
        assert tuple(
            connection.execute(
                """
                SELECT state, repetitions
                FROM vocabulary_cards
                WHERE id = ?
                """,
                (current.card.id,),
            ).fetchone()
        ) == ("review", 1)
        assert connection.execute(
            "SELECT status FROM study_sessions WHERE id = ?",
            (started_review.snapshot.session_id,),
        ).fetchone()[0] == "exited"
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


def create_v4_database(path: Path) -> None:
    create_v3_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(_migration("004_graded_reviews_and_tests.sql"))


def create_v5_database(path: Path) -> None:
    create_v4_database(path)
    database = Database(path)
    with database.connect() as connection:
        database._apply_migration(connection, 5)


def add_legacy_entry(
    connection: sqlite3.Connection,
    *,
    entry_id: int,
    sense_id: int,
    added_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO vocabulary_entries (
            id, display_text, normalized_text, date_added,
            last_reviewed, review_status
        ) VALUES (?, ?, ?, ?, NULL, 'new')
        """,
        (entry_id, f"word-{entry_id}", f"word-{entry_id}", added_at),
    )
    connection.execute(
        """
        INSERT INTO vocabulary_senses (
            id, entry_id, definition, part_of_speech, example_sentence,
            source_context, date_added
        ) VALUES (?, ?, ?, 'noun', ?, NULL, ?)
        """,
        (
            sense_id,
            entry_id,
            f"Definition {entry_id}.",
            f"Example {entry_id}.",
            added_at,
        ),
    )


def test_v5_to_v6_migration_preserves_entries_and_adds_empty_image_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v5_database(path)
    with sqlite3.connect(path) as connection:
        add_legacy_entry(
            connection,
            entry_id=41,
            sense_id=51,
            added_at="2026-08-01T00:00:00Z",
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'vocabulary_entry_images'"
        ).fetchone()[0] == 0

    Database(path).initialize()

    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert tuple(
            connection.execute(
                "SELECT id, display_text, normalized_text "
                "FROM vocabulary_entries WHERE id = 41"
            ).fetchone()
        ) == (41, "word-41", "word-41")
        assert tuple(
            connection.execute(
                "SELECT id, entry_id, definition "
                "FROM vocabulary_senses WHERE id = 51"
            ).fetchone()
        ) == (51, 41, "Definition 41.")
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entry_images"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM image_backfill_attempts"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v6_image_schema_enforces_association_receipt_and_cascade_invariants(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()

    with database.connect() as connection:
        add_legacy_entry(
            connection,
            entry_id=1,
            sense_id=11,
            added_at="2026-08-01T00:00:00Z",
        )
        add_legacy_entry(
            connection,
            entry_id=2,
            sense_id=22,
            added_at="2026-08-02T00:00:00Z",
        )
        insert_image = """
            INSERT INTO vocabulary_entry_images (
                entry_id, sense_id, category, query, description, origin,
                created_at, updated_at
            ) VALUES (?, ?, 'object', 'red teapot', 'a red teapot',
                      'capture', '2026-08-03T00:00:00Z',
                      '2026-08-03T00:00:00Z')
        """

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert_image, (2, 11))
        connection.execute(insert_image, (1, 11))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert_image, (1, 11))
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entry_images WHERE entry_id = 1"
        ).fetchone()[0] == 1

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE vocabulary_entry_images SET photo_url = ? WHERE entry_id = 1",
                ("https://upload.wikimedia.org/teapot.jpg",),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE vocabulary_entry_images
                SET telegram_file_id = ?, telegram_file_unique_id = ?
                WHERE entry_id = 1
                """,
                ("telegram-file", "telegram-unique"),
            )
        connection.execute(
            """
            UPDATE vocabulary_entry_images
            SET photo_url = ?, caption = ?, source_url = ?
            WHERE entry_id = 1
            """,
            (
                "https://upload.wikimedia.org/teapot.jpg",
                "A red teapot",
                "https://commons.wikimedia.org/wiki/File:Teapot.jpg",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE vocabulary_entry_images
                SET telegram_file_id = ?
                WHERE entry_id = 1
                """,
                ("telegram-file",),
            )
        connection.execute(
            """
            UPDATE vocabulary_entry_images
            SET telegram_file_id = ?, telegram_file_unique_id = ?
            WHERE entry_id = 1
            """,
            ("telegram-file", "telegram-unique"),
        )
        assert tuple(
            connection.execute(
                """
                SELECT photo_url, caption, source_url,
                       telegram_file_id, telegram_file_unique_id
                FROM vocabulary_entry_images WHERE entry_id = 1
                """
            ).fetchone()
        ) == (
            "https://upload.wikimedia.org/teapot.jpg",
            "A red teapot",
            "https://commons.wikimedia.org/wiki/File:Teapot.jpg",
            "telegram-file",
            "telegram-unique",
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_backfill_attempts (
                    entry_id, status, attempt_count, last_error, attempted_at
                ) VALUES (2, 'unknown', 1, 'bad status',
                          '2026-08-03T00:00:00Z')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO image_backfill_attempts (
                    entry_id, status, attempt_count, last_error, attempted_at
                ) VALUES (2, 'no_visual', 1, 'unexpected error',
                          '2026-08-03T00:00:00Z')
                """
            )
        connection.execute(
            """
            INSERT INTO image_backfill_attempts (
                entry_id, status, attempt_count, last_error, attempted_at
            ) VALUES (2, 'no_visual', 1, NULL, '2026-08-03T00:00:00Z')
            """
        )

        connection.execute("DELETE FROM vocabulary_entries WHERE id = 1")
        connection.execute("DELETE FROM vocabulary_entries WHERE id = 2")
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entry_images"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM image_backfill_attempts"
        ).fetchone()[0] == 0


def test_v5_domain_enums_match_persisted_constraint_values() -> None:
    assert {value.value for value in CardDirection} == {"forward", "reverse"}
    assert {value.value for value in StudyMode} == {
        "review",
        "test_forward",
        "test_reverse",
    }
    assert {value.value for value in StudySessionStatus} == {
        "active",
        "interrupted",
        "completed",
        "exited",
    }
    assert {value.value for value in StudyQueueStatus} == {
        "queued",
        "current",
        "completed",
        "skipped",
    }
    assert {value.value for value in StudyPromptStatus} == {
        "prepared",
        "delivered",
        "answered",
        "completed",
        "failed",
        "cancelled",
    }
    assert {value.value for value in DeliveryAttemptStatus} == {
        "unknown",
        "failed",
        "delivered",
    }


def test_v5_creates_one_forward_card_and_one_reverse_card_per_sense(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO vocabulary_senses (
                id, entry_id, definition, part_of_speech, example_sentence,
                source_context, date_added
            ) VALUES (23, 7, 'A template document.', 'noun', 'Use the pro forma.',
                      NULL, '2026-01-03T00:00:00Z')
            """
        )

    Database(path).initialize()

    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT direction, entry_id, sense_id, state, repetitions,
                       scheduler_kind, scheduler_version,
                       parameter_fingerprint, desired_retention
                FROM vocabulary_cards
                WHERE entry_id = 7
                ORDER BY direction, sense_id
                """
            )
        ] == [
            (
                "forward",
                7,
                None,
                "new",
                0,
                "fsrs-6",
                "fsrs-6.3.1-hermes-1",
                "sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56",
                0.9,
            ),
            (
                "reverse",
                7,
                21,
                "new",
                0,
                "fsrs-6",
                "fsrs-6.3.1-hermes-1",
                "sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56",
                0.9,
            ),
            (
                "reverse",
                7,
                22,
                "new",
                0,
                "fsrs-6",
                "fsrs-6.3.1-hermes-1",
                "sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56",
                0.9,
            ),
            (
                "reverse",
                7,
                23,
                "new",
                0,
                "fsrs-6",
                "fsrs-6.3.1-hermes-1",
                "sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56",
                0.9,
            ),
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v5_replays_only_valid_graded_forward_history_in_stable_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    reviewed_at = "2026-02-01T08:01:00Z"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE review_events
            SET grade = 'correct', evaluation_feedback = 'Correct.'
            WHERE id = 31
            """
        )
        connection.execute(
            """
            INSERT INTO review_events (
                id, entry_id, review_date, status, prompted_at, answered_at,
                answer_text, grade, evaluation_feedback
            ) VALUES (
                32, 7, '2026-02-02', 'answered',
                '2026-02-01T08:00:00Z', ?, 'partial answer',
                'partial', 'Incomplete.'
            )
            """,
            (reviewed_at,),
        )
        connection.execute(
            """
            INSERT INTO test_sessions (id, status, started_at, completed_at)
            VALUES (41, 'completed', '2026-02-01T07:00:00Z',
                    '2026-02-01T09:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO test_questions (
                id, session_id, position, entry_id, answer_text,
                grade, evaluation_feedback, answered_at
            ) VALUES (
                51, 41, 1, 7, 'wrong answer', 'incorrect', 'Incorrect.', ?
            )
            """,
            (reviewed_at,),
        )
        connection.execute(
            """
            INSERT INTO review_events (
                id, entry_id, review_date, status, prompted_at, answered_at,
                answer_text, grade, evaluation_feedback
            ) VALUES (
                33, 7, '2026-02-03', 'answered',
                '2026-02-03T08:00:00Z', '2026-02-03T09:01:00+01:00',
                'non UTC', 'correct', 'Should be ignored.'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO review_events (
                id, entry_id, review_date, status, prompted_at, answered_at,
                answer_text, grade, evaluation_feedback
            ) VALUES (
                34, 7, '2026-02-04', 'answered',
                '2026-02-04T08:00:00Z', '2026-02-04T08:01:00Z',
                'ungraded', NULL, NULL
            )
            """
        )

    Database(path).initialize()

    with Database(path).connect() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT legacy_source, legacy_id, rating,
                       before_repetitions, after_repetitions
                FROM review_attempts
                ORDER BY reviewed_at, legacy_source, legacy_id
                """
            )
        ] == [
            ("review_event", 31, "good", 0, 1),
            ("review_event", 32, "again", 1, 2),
            ("test_question", 51, "again", 2, 3),
        ]
        assert tuple(
            connection.execute(
                """
                SELECT state, repetitions, lapses, last_review_at
                FROM vocabulary_cards
                WHERE entry_id = 7 AND direction = 'forward'
                """
            ).fetchone()
        ) == ("relearning", 3, 2, reviewed_at)
        assert connection.execute(
            "SELECT COUNT(*) FROM review_attempts"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM study_prompts"
        ).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE review_attempts SET rating = 'easy' WHERE legacy_id = 31"
            )


def test_v5_pending_and_missed_history_create_due_non_answerable_cards(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE review_events
            SET grade = 'correct', evaluation_feedback = 'Correct.'
            WHERE id = 31
            """
        )
        connection.execute(
            """
            INSERT INTO review_events (
                id, entry_id, review_date, status, prompted_at,
                answered_at, answer_text, grade, evaluation_feedback
            ) VALUES (
                32, 7, '2026-02-02', 'pending', '2026-02-02T08:00:00Z',
                NULL, NULL, NULL, NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO review_events (
                id, entry_id, review_date, status, prompted_at,
                answered_at, answer_text, grade, evaluation_feedback
            ) VALUES (
                33, 7, '2026-02-03', 'missed', '2026-02-03T08:00:00Z',
                NULL, NULL, NULL, NULL
            )
            """
        )

    Database(path).initialize()

    with Database(path).connect() as connection:
        card = connection.execute(
            """
            SELECT state, due_at, effective_due_at
            FROM vocabulary_cards
            WHERE entry_id = 7 AND direction = 'forward'
            """
        ).fetchone()
        assert tuple(card) == (
            "review",
            "2026-02-03T08:01:00Z",
            "2026-02-02T08:00:00Z",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM review_attempts"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM study_prompts"
        ).fetchone()[0] == 0


def test_v5_reconstructs_active_legacy_test_without_duplicate_attempt_or_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    with sqlite3.connect(path) as connection:
        add_legacy_entry(
            connection,
            entry_id=8,
            sense_id=81,
            added_at="2026-01-02T00:00:00Z",
        )
        add_legacy_entry(
            connection,
            entry_id=9,
            sense_id=91,
            added_at="2026-01-03T00:00:00Z",
        )
        connection.execute(
            """
            INSERT INTO test_sessions (id, status, started_at, completed_at)
            VALUES (42, 'active', '2026-02-05T23:30:00Z', NULL)
            """
        )
        connection.executemany(
            """
            INSERT INTO test_questions (
                id, session_id, position, entry_id, answer_text,
                grade, evaluation_feedback, answered_at
            ) VALUES (?, 42, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    61,
                    1,
                    7,
                    "a projected statement",
                    "correct",
                    "Correct.",
                    "2026-02-05T23:31:00Z",
                ),
                (62, 2, 8, None, None, None, None),
                (63, 3, 9, None, None, None, None),
            ),
        )
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")

    database = Database(path)
    database.initialize()
    database.initialize()

    with database.connect() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT mode, status, legacy_test_session_id, local_date
                FROM study_sessions
                """
            )
        ] == [("test_forward", "interrupted", 42, "2026-02-06")]
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT q.position, q.status, q.legacy_test_question_id,
                       a.legacy_id
                FROM study_queue q
                LEFT JOIN review_attempts a ON a.id = q.completed_attempt_id
                ORDER BY q.position
                """
            )
        ] == [
            (1, "completed", 61, 61),
            (2, "queued", 62, None),
            (3, "queued", 63, None),
        ]
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT entry_id, introduced_local_date
                FROM vocabulary_cards
                WHERE direction = 'forward' AND entry_id IN (8, 9)
                ORDER BY entry_id
                """
            )
        ] == [(8, "2026-02-06"), (9, "2026-02-06")]
        assert connection.execute(
            "SELECT COUNT(*) FROM review_attempts WHERE legacy_source = 'test_question'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM study_prompts"
        ).fetchone()[0] == 0


def test_v5_enforces_direction_session_prompt_attempt_and_retry_uniqueness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    Database(path).initialize()

    with Database(path).connect() as connection:
        forward_card_id = connection.execute(
            """
            SELECT id FROM vocabulary_cards
            WHERE entry_id = 7 AND direction = 'forward'
            """
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO vocabulary_cards (
                    entry_id, direction, state, due_at, effective_due_at,
                    repetitions, lapses, scheduler_kind, scheduler_version,
                    parameters_version, parameter_fingerprint,
                    desired_retention, created_at
                )
                SELECT entry_id, direction, state, due_at, effective_due_at,
                       repetitions, lapses, scheduler_kind, scheduler_version,
                       parameters_version, parameter_fingerprint,
                       desired_retention, created_at
                FROM vocabulary_cards WHERE id = ?
                """,
                (forward_card_id,),
            )
        connection.execute(
            """
            INSERT INTO study_sessions (mode, status, started_at, local_date)
            VALUES ('review', 'active', '2026-02-10T08:00:00Z', '2026-02-10')
            """
        )
        session_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO study_sessions (mode, status, started_at, local_date)
                VALUES ('test_forward', 'active',
                        '2026-02-10T08:00:01Z', '2026-02-10')
                """
            )
        connection.execute(
            """
            INSERT INTO study_queue (session_id, card_id, position, status)
            VALUES (?, ?, 1, 'current')
            """,
            (session_id, forward_card_id),
        )
        queue_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """
            INSERT INTO study_prompts (
                session_id, queue_item_id, prompt_key, prompt_text,
                status, prepared_at
            ) VALUES (?, ?, 'prompt-1', 'Define pro forma.',
                      'prepared', '2026-02-10T08:00:00Z')
            """,
            (session_id, queue_id),
        )
        prompt_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO study_prompts (
                    session_id, queue_item_id, prompt_key, prompt_text,
                    status, prepared_at
                ) VALUES (?, ?, 'prompt-2', 'Duplicate active prompt.',
                          'prepared', '2026-02-10T08:00:01Z')
                """,
                (session_id, queue_id),
            )
        connection.execute(
            """
            INSERT INTO prompt_delivery_attempts (
                prompt_id, attempt_number, status, attempted_at,
                receipt_at, outbound_delivery_id
            ) VALUES (
                ?, 1, 'delivered', '2026-02-10T08:00:02Z',
                '2026-02-10T08:00:03Z', 'delivery-1'
            )
            """,
            (prompt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE prompt_delivery_attempts SET status = 'failed' WHERE prompt_id = ?",
                (prompt_id,),
            )
        connection.execute(
            """
            UPDATE study_prompts
            SET status = 'delivered', delivered_at = '2026-02-10T08:00:03Z'
            WHERE id = ?
            """,
            (prompt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="cannot regress"):
            connection.execute(
                "UPDATE study_prompts SET status = 'prepared' WHERE id = ?",
                (prompt_id,),
            )
        connection.execute(
            """
            UPDATE study_prompts
            SET status = 'answered', answered_at = '2026-02-10T08:01:00Z'
            WHERE id = ?
            """,
            (prompt_id,),
        )
        connection.execute(
            """
            INSERT INTO answer_drafts (
                prompt_id, submitted_answer, evaluator_grade,
                evaluation_feedback, answered_at, created_at
            ) VALUES (
                ?, 'A projected statement.', 'correct', 'Correct.',
                '2026-02-10T08:01:00Z', '2026-02-10T08:01:01Z'
            )
            """,
            (prompt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE answer_drafts
                SET submitted_answer = 'Changed answer.'
                WHERE prompt_id = ?
                """,
                (prompt_id,),
            )
        connection.execute(
            """
            INSERT INTO study_queue (session_id, card_id, position, status)
            VALUES (?, ?, 10, 'queued')
            """,
            (session_id, forward_card_id),
        )
        second_queue_id = connection.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO study_prompts (
                    session_id, queue_item_id, prompt_key, prompt_text,
                    status, prepared_at
                ) VALUES (?, ?, 'prompt-after-answer', 'Must remain blocked.',
                          'prepared', '2026-02-10T08:01:02Z')
                """,
                (session_id, second_queue_id),
            )
        connection.execute(
            "UPDATE study_prompts SET status = 'completed' WHERE id = ?",
            (prompt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="terminal"):
            connection.execute(
                "UPDATE study_prompts SET status = 'cancelled' WHERE id = ?",
                (prompt_id,),
            )
        connection.execute(
            """
            INSERT INTO study_queue (
                session_id, card_id, position, status, retry_of_queue_item_id
            ) VALUES (?, ?, 2, 'queued', ?)
            """,
            (session_id, forward_card_id, queue_id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO study_queue (
                    session_id, card_id, position, status, retry_of_queue_item_id
                ) VALUES (?, ?, 3, 'queued', ?)
                """,
                (session_id, forward_card_id, queue_id),
            )
        connection.rollback()


def test_v5_backfill_failure_rolls_back_schema_data_and_version_then_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import hermes_vocab.database as database_module

    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    original = database_module._MIGRATION_BACKFILLS[5]

    def failing_backfill(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected v5 backfill failure")

    monkeypatch.setitem(database_module._MIGRATION_BACKFILLS, 5, failing_backfill)
    with pytest.raises(RuntimeError, match="injected v5 backfill failure"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'vocabulary_cards'
            """
        ).fetchone()[0] == 0

    monkeypatch.setitem(database_module._MIGRATION_BACKFILLS, 5, original)
    Database(path).initialize()
    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_cards"
        ).fetchone()[0] == 3


def test_v5_sql_failure_rolls_back_schema_and_version_then_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    original = Database._migration_sql
    broken_v5 = """
    CREATE TABLE partial_v5 (id INTEGER PRIMARY KEY);
    INSERT INTO missing_v5_table VALUES (1);
    PRAGMA user_version = 5;
    """
    monkeypatch.setattr(
        Database,
        "_migration_sql",
        staticmethod(lambda target: broken_v5 if target == 5 else original(target)),
    )

    with pytest.raises(sqlite3.OperationalError):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'partial_v5'
            """
        ).fetchone()[0] == 0

    monkeypatch.setattr(Database, "_migration_sql", staticmethod(original))
    Database(path).initialize()
    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_concurrent_v4_initialization_runs_v5_backfill_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import hermes_vocab.database as database_module

    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v4_database(path)
    original = database_module._MIGRATION_BACKFILLS[5]
    calls: list[int] = []

    def counting_backfill(connection: sqlite3.Connection) -> None:
        calls.append(1)
        original(connection)

    monkeypatch.setitem(database_module._MIGRATION_BACKFILLS, 5, counting_backfill)
    initialize_concurrently(path, monkeypatch, synchronized_target=5)

    assert calls == [1]
    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
