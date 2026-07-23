from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from hermes_vocab.capture import CaptureService
from hermes_vocab.database import Database
from hermes_vocab.models import (
    CaptureCommand,
    CaptureOperation,
    Evaluation,
    EvaluationGrade,
    SenseCard,
    TestCompletionStatus as CompletionStatus,
    TestSessionStatus as SessionStatus,
    TestSnapshotStatus as SnapshotStatus,
    TestStartStatus as StartStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService as SessionService
from zoneinfo import ZoneInfo


NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def setup_service(tmp_path: Path) -> tuple[SessionService, Database, Clock]:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    clock = Clock(NOW)
    return SessionService(database, clock), database, clock


def add_entries(database: Database, count: int) -> None:
    for index in range(count):
        captured_at = NOW + timedelta(minutes=index)
        CaptureService(database, clock=lambda value=captured_at: value).capture(
            CaptureCommand(
                display_text=f"word-{index}",
                operation=CaptureOperation.NEW_ENTRY,
                card=SenseCard(
                    part_of_speech="noun",
                    definition=f"Definition {index}.",
                    example_sentence=f"Example {index}.",
                ),
            )
        )


def evaluate(grade: EvaluationGrade, feedback: str = "Useful feedback.") -> Evaluation:
    return Evaluation(grade=grade, feedback=feedback)


def complete_active_session(service: SessionService) -> None:
    for _ in range(5):
        current = service.current().snapshot
        assert current is not None and current.current_question is not None
        service.complete(
            current.current_question.id,
            "answer",
            evaluate(EvaluationGrade.CORRECT),
        )


def test_start_creates_exactly_five_ordered_distinct_questions_without_review_mutation(
    tmp_path: Path,
) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 6)

    result = service.start()

    assert result.status is StartStatus.STARTED
    assert result.snapshot is not None
    assert result.snapshot.session.status is SessionStatus.ACTIVE
    assert [question.position for question in result.snapshot.questions] == [1, 2, 3, 4, 5]
    assert [question.entry.display_text for question in result.snapshot.questions] == [
        "word-0",
        "word-1",
        "word-2",
        "word-3",
        "word-4",
    ]
    assert len({question.entry.id for question in result.snapshot.questions}) == 5
    assert result.snapshot.current_question == result.snapshot.questions[0]
    assert result.snapshot.summary.correct == 0
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM test_questions").fetchone()[0] == 5
        assert [tuple(row) for row in connection.execute(
            "SELECT last_reviewed, review_status FROM vocabulary_entries ORDER BY id"
        )] == [(None, "new")] * 6


def test_next_test_prioritizes_entries_never_used_in_a_test(tmp_path: Path) -> None:
    service, database, clock = setup_service(tmp_path)
    add_entries(database, 7)
    first = service.start().snapshot
    assert first is not None
    assert [question.entry.display_text for question in first.questions] == [
        "word-0",
        "word-1",
        "word-2",
        "word-3",
        "word-4",
    ]
    complete_active_session(service)
    clock.value += timedelta(hours=1)

    second = service.start().snapshot

    assert second is not None
    assert [question.entry.display_text for question in second.questions] == [
        "word-5",
        "word-6",
        "word-0",
        "word-1",
        "word-2",
    ]


def test_test_rotation_cycles_oldest_tested_entries_without_review_mutation(
    tmp_path: Path,
) -> None:
    service, database, clock = setup_service(tmp_path)
    add_entries(database, 10)

    first = service.start().snapshot
    assert first is not None
    assert [question.entry.display_text for question in first.questions] == [
        "word-0",
        "word-1",
        "word-2",
        "word-3",
        "word-4",
    ]
    complete_active_session(service)
    clock.value += timedelta(hours=1)

    second = service.start().snapshot
    assert second is not None
    assert [question.entry.display_text for question in second.questions] == [
        "word-5",
        "word-6",
        "word-7",
        "word-8",
        "word-9",
    ]
    complete_active_session(service)
    clock.value += timedelta(hours=1)

    third = service.start().snapshot
    assert third is not None
    assert [question.entry.display_text for question in third.questions] == [
        "word-0",
        "word-1",
        "word-2",
        "word-3",
        "word-4",
    ]
    with database.connect() as connection:
        scheduling = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT last_reviewed, review_status
                FROM vocabulary_entries
                ORDER BY id
                """
            )
        ]
    assert scheduling == [(None, "new")] * 10


@pytest.mark.parametrize("available", range(5))
def test_start_with_insufficient_library_creates_nothing(
    tmp_path: Path,
    available: int,
) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, available)

    result = service.start()

    assert result.status is StartStatus.INSUFFICIENT_LIBRARY
    assert result.available_count == available
    assert result.required_count == 5
    assert result.snapshot is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM test_questions").fetchone()[0] == 0


def test_entries_without_senses_do_not_count_toward_five_valid_entries(
    tmp_path: Path,
) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 4)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO vocabulary_entries (
                display_text, normalized_text, date_added
            ) VALUES ('incomplete', 'incomplete', '2026-07-19T12:10:00Z')
            """
        )
        connection.commit()

    result = service.start()

    assert result.status is StartStatus.INSUFFICIENT_LIBRARY
    assert result.available_count == 4


def test_duplicate_start_resumes_same_session_and_current_question(tmp_path: Path) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 5)
    first = service.start()
    first_question = first.snapshot.current_question
    service.complete(first_question.id, "answer one", evaluate(EvaluationGrade.CORRECT))

    duplicate = service.start()

    assert duplicate.status is StartStatus.RESUMED
    assert duplicate.snapshot.session.id == first.snapshot.session.id
    assert duplicate.snapshot.current_question.position == 2
    assert duplicate.snapshot.summary.correct == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM test_questions").fetchone()[0] == 5


def test_completion_persists_raw_history_grades_feedback_and_summary(tmp_path: Path) -> None:
    service, database, clock = setup_service(tmp_path)
    add_entries(database, 5)
    started = service.start()
    grades = [
        EvaluationGrade.CORRECT,
        EvaluationGrade.PARTIAL,
        EvaluationGrade.INCORRECT,
        EvaluationGrade.CORRECT,
        EvaluationGrade.PARTIAL,
    ]
    answers = ["  raw one  ", "raw two", "raw three", "raw four", "raw five"]

    for index, (grade, answer) in enumerate(zip(grades, answers, strict=True), start=1):
        current = service.current().snapshot.current_question
        clock.value = NOW + timedelta(minutes=index)
        result = service.complete(
            current.id,
            answer,
            evaluate(grade, f"Feedback {index}."),
        )
        expected = (
            CompletionStatus.COMPLETED
            if index == 5
            else CompletionStatus.ADVANCED
        )
        assert result.status is expected

    assert result.snapshot.session.status is SessionStatus.COMPLETED
    assert result.snapshot.current_question is None
    assert result.snapshot.summary.correct == 2
    assert result.snapshot.summary.partial == 2
    assert result.snapshot.summary.incorrect == 1
    with database.connect() as connection:
        attempts = [tuple(row) for row in connection.execute(
            """
            SELECT position, answer_text, grade, evaluation_feedback, answered_at
            FROM test_questions ORDER BY position
            """
        )]
        entries = [tuple(row) for row in connection.execute(
            "SELECT last_reviewed, review_status FROM vocabulary_entries ORDER BY id"
        )]
    assert [row[:4] for row in attempts] == [
        (1, "  raw one  ", "correct", "Feedback 1."),
        (2, "raw two", "partial", "Feedback 2."),
        (3, "raw three", "incorrect", "Feedback 3."),
        (4, "raw four", "correct", "Feedback 4."),
        (5, "raw five", "partial", "Feedback 5."),
    ]
    assert all(row[4] is not None for row in attempts)
    assert entries == [(None, "new")] * 5


def test_completed_history_is_preserved_when_later_session_starts(tmp_path: Path) -> None:
    service, database, clock = setup_service(tmp_path)
    add_entries(database, 5)
    first = service.start().snapshot
    for _ in range(5):
        current = service.current().snapshot.current_question
        service.complete(current.id, f"answer {current.position}", evaluate(EvaluationGrade.CORRECT))

    clock.value = NOW + timedelta(days=1)
    second = service.start()

    assert second.status is StartStatus.STARTED
    assert second.snapshot.session.id != first.session.id
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM test_questions").fetchone()[0] == 10
        assert connection.execute(
            "SELECT COUNT(*) FROM test_questions WHERE answer_text IS NOT NULL"
        ).fetchone()[0] == 5


def test_restart_reconstructs_order_current_question_and_totals(tmp_path: Path) -> None:
    service, database, clock = setup_service(tmp_path)
    add_entries(database, 5)
    started = service.start().snapshot
    for grade in (EvaluationGrade.CORRECT, EvaluationGrade.INCORRECT):
        current = service.current().snapshot.current_question
        service.complete(current.id, f"raw {current.position}", evaluate(grade))

    restarted = SessionService(database, clock)
    current = restarted.current()

    assert current.status is SnapshotStatus.ACTIVE
    assert current.snapshot.session.id == started.session.id
    assert [question.entry.id for question in current.snapshot.questions] == [
        question.entry.id for question in started.questions
    ]
    assert current.snapshot.current_question.position == 3
    assert current.snapshot.summary.correct == 1
    assert current.snapshot.summary.partial == 0
    assert current.snapshot.summary.incorrect == 1


@pytest.mark.parametrize(
    ("answer", "evaluation"),
    [
        ("", evaluate(EvaluationGrade.CORRECT)),
        ("   ", evaluate(EvaluationGrade.CORRECT)),
        ("answer", None),
        ("answer", Evaluation(grade=EvaluationGrade.CORRECT, feedback="   ")),
    ],
)
def test_invalid_attempt_leaves_current_question_pending(
    tmp_path: Path,
    answer: str,
    evaluation: Evaluation | None,
) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 5)
    question = service.start().snapshot.current_question

    result = service.complete(question.id, answer, evaluation)

    assert result.status is CompletionStatus.INVALID
    assert service.current().snapshot.current_question.id == question.id
    with database.connect() as connection:
        row = connection.execute(
            "SELECT answer_text, grade, evaluation_feedback, answered_at FROM test_questions WHERE id = ?",
            (question.id,),
        ).fetchone()
    assert tuple(row) == (None, None, None, None)


def test_stale_or_concurrent_completion_advances_only_once(tmp_path: Path) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 5)
    question = service.start().snapshot.current_question

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda answer: service.complete(
                    question.id,
                    answer,
                    evaluate(EvaluationGrade.CORRECT),
                ),
                ("first raw", "second raw"),
            )
        )

    assert sorted(result.status for result in results) == sorted(
        (CompletionStatus.ADVANCED, CompletionStatus.STALE)
    )
    assert service.current().snapshot.current_question.position == 2
    with database.connect() as connection:
        attempts = connection.execute(
            "SELECT COUNT(*) FROM test_questions WHERE answer_text IS NOT NULL"
        ).fetchone()[0]
    assert attempts == 1


def test_fifth_attempt_and_session_completion_roll_back_together_on_storage_failure(
    tmp_path: Path,
) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 5)
    service.start()
    for _ in range(4):
        current = service.current().snapshot.current_question
        service.complete(current.id, "answer", evaluate(EvaluationGrade.CORRECT))
    fifth = service.current().snapshot.current_question
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_session_completion
            BEFORE UPDATE OF status ON test_sessions
            WHEN NEW.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'injected completion failure');
            END
            """
        )
        connection.commit()

    result = service.complete(fifth.id, "last raw", evaluate(EvaluationGrade.PARTIAL))

    assert result.status is CompletionStatus.STORAGE_ERROR
    resumed = service.current().snapshot
    assert resumed.current_question.id == fifth.id
    assert resumed.summary.correct == 4
    assert resumed.summary.partial == 0
    with database.connect() as connection:
        assert tuple(connection.execute(
            "SELECT answer_text, grade, answered_at FROM test_questions WHERE id = ?",
            (fifth.id,),
        ).fetchone()) == (None, None, None)
        assert connection.execute(
            "SELECT status FROM test_sessions WHERE id = ?", (resumed.session.id,)
        ).fetchone()[0] == "active"


def test_pending_daily_review_blocks_test_start(tmp_path: Path) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 5)
    review = ReviewService(database, ZoneInfo("UTC"), lambda: NOW)
    review.daily_review()

    result = service.start()

    assert result.status is StartStatus.DAILY_REVIEW_PENDING
    assert result.snapshot is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 0


def test_concurrent_starts_converge_on_one_session(tmp_path: Path) -> None:
    service, database, _ = setup_service(tmp_path)
    add_entries(database, 5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.start(), range(2)))

    assert {result.status for result in results} == {
        StartStatus.STARTED,
        StartStatus.RESUMED,
    }
    assert results[0].snapshot.session.id == results[1].snapshot.session.id
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM test_questions").fetchone()[0] == 5
