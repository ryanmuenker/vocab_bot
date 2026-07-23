from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_vocab.capture import CaptureService
from hermes_vocab.database import Database
from hermes_vocab.formatting import format_review_completion
from hermes_vocab.models import (
    EvaluationGrade,
    CaptureCommand,
    CaptureOperation,
    Evaluation,
    ReviewCompletionStatus,
    ReviewPromptStatus,
    SenseCard,
)
from hermes_vocab.review import PendingReviewStatus, ReviewService


NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


EVALUATION = Evaluation(
    grade=EvaluationGrade.CORRECT,
    feedback="Accurate meaning.",
)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def setup_service(tmp_path: Path, now: datetime) -> tuple[ReviewService, Clock, Database]:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    clock = Clock(now)
    service = ReviewService(database, ZoneInfo("America/New_York"), clock)
    return service, clock, database


def add_word(database: Database, word: str, now: datetime) -> None:
    CaptureService(database, clock=lambda: now).capture(
        CaptureCommand(display_text=word, operation=CaptureOperation.NEW_ENTRY,
        card=SenseCard(
            "adjective",
            f"Definition of {word}.",
            f"Example with {word}.",
        ),)
    )


def add_bank_with_two_senses(database: Database) -> None:
    capture = CaptureService(database, clock=lambda: NOW)
    capture.capture(
        CaptureCommand(display_text="bank", operation=CaptureOperation.NEW_ENTRY,
        card=SenseCard(
            part_of_speech="noun",
            definition="A financial institution.",
            example_sentence="She deposited the cheque at the bank.",
        ),)
    )
    capture.capture(
        CaptureCommand(display_text="bank", operation=CaptureOperation.NEW_SENSE,
        card=SenseCard(
            part_of_speech="noun",
            definition="Land alongside a river.",
            example_sentence="They rested on the grassy bank.",
        ),
        source_context="They rested on the bank beside the river.",)
    )


def test_daily_review_creates_one_pending_word_event_with_all_senses(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_bank_with_two_senses(database)

    result = service.daily_review()

    assert result.status is ReviewPromptStatus.PENDING
    assert result.entry is not None
    assert result.entry.display_text == "bank"
    assert len(result.entry.senses) == 2
    assert result.event is not None
    assert result.event.entry_id == result.entry.id
    assert result.event.review_date.isoformat() == "2026-07-16"


def test_review_reveal_keeps_senses_in_insertion_order_with_reversed_clocks(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    timestamps = iter((NOW, NOW - timedelta(days=1)))
    capture = CaptureService(database, clock=lambda: next(timestamps))
    capture.capture(
        CaptureCommand(display_text="bank", operation=CaptureOperation.NEW_ENTRY,
        card=SenseCard(
            part_of_speech="noun",
            definition="A financial institution.",
            example_sentence="She deposited the cheque at the bank.",
        ),)
    )
    capture.capture(
        CaptureCommand(display_text="bank", operation=CaptureOperation.NEW_SENSE,
        card=SenseCard(
            part_of_speech="noun",
            definition="Land alongside a river.",
            example_sentence="They rested on the grassy bank.",
        ),)
    )
    pending = service.daily_review()

    result = service.complete_review(pending.event.id, "my answer", EVALUATION)

    assert result.status is ReviewCompletionStatus.COMPLETED
    assert [sense.definition for sense in result.entry.senses] == [
        "A financial institution.",
        "Land alongside a river.",
    ]
    reveal = format_review_completion(result)
    assert reveal.index("1. noun — A financial institution.") < reveal.index(
        "2. noun — Land alongside a river."
    )


def test_same_day_pending_is_idempotent_and_answered_is_silent(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)

    first = service.daily_review()
    second = service.daily_review()
    completed = service.complete_review(
        first.event.id,
        "brief and direct",
        EVALUATION,
    )
    after_answer = service.daily_review()

    assert second.event == first.event
    assert second.entry == first.entry
    assert completed.status is ReviewCompletionStatus.COMPLETED
    assert completed.answer_text == "brief and direct"
    assert completed.entry is not None
    assert completed.entry.last_reviewed == NOW
    assert completed.entry.review_status == "reviewed"
    assert after_answer.status is ReviewPromptStatus.ALREADY_COMPLETED


def test_next_day_marks_unanswered_review_missed(tmp_path: Path) -> None:
    first_day = NOW
    service, clock, database = setup_service(tmp_path, first_day)
    add_word(database, "laconic", first_day)
    add_word(database, "obdurate", first_day)
    first = service.daily_review()

    clock.value = datetime(2026, 7, 17, 12, tzinfo=UTC)
    second = service.daily_review()

    with database.connect() as connection:
        first_status = connection.execute(
            "SELECT status FROM review_events WHERE id = ?", (first.event.id,)
        ).fetchone()[0]
    assert first_status == "missed"
    assert second.status is ReviewPromptStatus.PENDING
    assert second.event.review_date.isoformat() == "2026-07-17"


def test_review_prioritizes_oldest_never_reviewed_word(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "first", datetime(2026, 7, 1, tzinfo=UTC))
    add_word(database, "second", datetime(2026, 7, 2, tzinfo=UTC))

    assert service.daily_review().entry.display_text == "first"


def test_empty_library_creates_no_event(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)

    result = service.daily_review()

    assert result.status is ReviewPromptStatus.EMPTY
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 0


def test_blank_absent_evaluation_or_missing_review_does_not_change_state(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)
    pending = service.daily_review()

    assert (
        service.complete_review(pending.event.id, " ", EVALUATION).status
        is ReviewCompletionStatus.INVALID
    )
    assert (
        service.complete_review(pending.event.id, "answer", None).status
        is ReviewCompletionStatus.INVALID
    )
    assert (
        service.complete_review(
            pending.event.id,
            "answer",
            Evaluation(EvaluationGrade.CORRECT, " "),
        ).status
        is ReviewCompletionStatus.INVALID
    )
    with database.connect() as connection:
        assert tuple(connection.execute(
            "SELECT status, answer_text, grade, evaluation_feedback FROM review_events"
        ).fetchone()) == ("pending", None, None, None)


def test_pending_review_survives_service_restart(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)
    first = service.daily_review()

    restarted = ReviewService(database, ZoneInfo("America/New_York"), lambda: NOW)
    resumed = restarted.daily_review()
    completed = restarted.complete_review(
        resumed.event.id,
        "Using few words.",
        EVALUATION,
    )

    assert resumed.event == first.event
    assert resumed.entry == first.entry
    assert completed.status is ReviewCompletionStatus.COMPLETED
    assert completed.entry.display_text == "laconic"


def test_pending_review_snapshot_is_read_only_and_restart_safe(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)
    created = service.daily_review()

    restarted = ReviewService(
        database,
        ZoneInfo("America/New_York"),
        lambda: NOW + timedelta(hours=1),
    )
    pending = restarted.pending_review()

    assert pending.status is PendingReviewStatus.PENDING
    assert pending.event == created.event
    assert pending.entry == created.entry
    with database.connect() as connection:
        assert [tuple(row) for row in connection.execute(
            """
            SELECT id, status, prompted_at, answered_at, answer_text, grade
            FROM review_events
            """
        )] == [
            (
                created.event.id,
                "pending",
                NOW.isoformat().replace("+00:00", "Z"),
                None,
                None,
                None,
            )
        ]


def test_concurrent_same_day_creation_produces_one_event(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.daily_review(), range(2)))

    assert all(result.status is ReviewPromptStatus.PENDING for result in results)
    assert results[0].event == results[1].event
    assert results[0].entry == results[1].entry
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 1


def test_pending_review_status_distinguishes_pending_none_and_storage_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    assert service.pending_review_status() is PendingReviewStatus.NONE

    add_word(database, "laconic", NOW)
    service.daily_review()
    assert service.pending_review_status() is PendingReviewStatus.PENDING

    def fail_connect():
        raise sqlite3.OperationalError("private state")

    monkeypatch.setattr(database, "connect", fail_connect)
    assert service.pending_review_status() is PendingReviewStatus.STORAGE_ERROR


def test_evaluated_completion_persists_each_grade_feedback_and_raw_answer(
    tmp_path: Path,
) -> None:
    for offset, grade in enumerate(EvaluationGrade):
        now = NOW + timedelta(days=offset)
        service, _, database = setup_service(tmp_path / str(offset), now)
        add_word(database, f"word-{offset}", now)
        pending = service.daily_review()

        result = service.complete_review(
            pending.event.id,
            "  learner raw answer  ",
            Evaluation(grade, f"Feedback for {grade.value}."),
        )

        assert result.status is ReviewCompletionStatus.COMPLETED
        assert result.answer_text == "  learner raw answer  "
        assert result.grade is grade
        assert result.feedback == f"Feedback for {grade.value}."
        with database.connect() as connection:
            event = connection.execute(
                """
                SELECT status, answer_text, grade, evaluation_feedback, answered_at
                FROM review_events WHERE id = ?
                """,
                (pending.event.id,),
            ).fetchone()
            entry = connection.execute(
                "SELECT last_reviewed, review_status FROM vocabulary_entries"
            ).fetchone()
        assert tuple(event[:4]) == (
            "answered",
            "  learner raw answer  ",
            grade.value,
            f"Feedback for {grade.value}.",
        )
        assert event["answered_at"] is not None
        assert tuple(entry) == (
            now.isoformat().replace("+00:00", "Z"),
            "reviewed",
        )


def test_guarded_review_completion_commits_only_one_concurrent_evaluation(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)
    pending = service.daily_review()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda answer: service.complete_review(
                pending.event.id,
                answer,
                EVALUATION,
            ),
            ("first raw", "second raw"),
        ))

    assert sorted(result.status for result in results) == sorted(
        (ReviewCompletionStatus.COMPLETED, ReviewCompletionStatus.NO_PENDING)
    )
    with database.connect() as connection:
        event = connection.execute(
            "SELECT status, answer_text, grade FROM review_events WHERE id = ?",
            (pending.event.id,),
        ).fetchone()
    assert event["status"] == "answered"
    assert event["answer_text"] in {"first raw", "second raw"}
    assert event["grade"] == "correct"


def test_active_test_suppresses_daily_review_without_inserting_event(
    tmp_path: Path,
) -> None:
    from hermes_vocab.test_session import TestSessionService

    service, _, database = setup_service(tmp_path, NOW)
    for index in range(5):
        add_word(database, f"word-{index}", NOW + timedelta(minutes=index))
    TestSessionService(database, lambda: NOW).start()

    result = service.daily_review()

    assert result.status is ReviewPromptStatus.TEST_ACTIVE
    assert result.event is None
    assert result.entry is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 0


def test_completed_test_before_morning_review_allows_pending_review(
    tmp_path: Path,
) -> None:
    from hermes_vocab.test_session import TestSessionService

    review, _, database = setup_service(tmp_path, NOW)
    for index in range(5):
        add_word(database, f"word-{index}", NOW + timedelta(minutes=index))
    tests = TestSessionService(database, lambda: NOW)
    tests.start()
    for _ in range(5):
        question = tests.current().snapshot.current_question
        tests.complete(
            question.id,
            f"answer {question.position}",
            Evaluation(EvaluationGrade.CORRECT, "Accurate."),
        )

    result = review.daily_review()

    assert result.status is ReviewPromptStatus.PENDING
    assert result.event is not None
    assert result.entry is not None
    with database.connect() as connection:
        event = connection.execute(
            "SELECT status FROM review_events"
        ).fetchone()
    assert event["status"] == "pending"
