from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_vocab.capture import CaptureService
from hermes_vocab.database import Database
from hermes_vocab.formatting import format_review_completion
from hermes_vocab.models import (
    CaptureCommand,
    CaptureOperation,
    ReviewCompletionStatus,
    ReviewPromptStatus,
    SenseCard,
)
from hermes_vocab.review import ReviewService


NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


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
        CaptureCommand(
            word=word,
            operation=CaptureOperation.NEW_WORD,
            card=SenseCard(
                "adjective",
                f"Definition of {word}.",
                f"Example with {word}.",
            ),
        )
    )


def add_bank_with_two_senses(database: Database) -> None:
    capture = CaptureService(database, clock=lambda: NOW)
    capture.capture(
        CaptureCommand(
            word="bank",
            operation=CaptureOperation.NEW_WORD,
            card=SenseCard(
                part_of_speech="noun",
                definition="A financial institution.",
                example_sentence="She deposited the cheque at the bank.",
            ),
        )
    )
    capture.capture(
        CaptureCommand(
            word="bank",
            operation=CaptureOperation.NEW_SENSE,
            card=SenseCard(
                part_of_speech="noun",
                definition="Land alongside a river.",
                example_sentence="They rested on the grassy bank.",
            ),
            source_context="They rested on the bank beside the river.",
        )
    )


def test_daily_review_creates_one_pending_word_event_with_all_senses(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_bank_with_two_senses(database)

    result = service.daily_review()

    assert result.status is ReviewPromptStatus.PENDING
    assert result.word is not None
    assert result.word.word == "bank"
    assert len(result.word.senses) == 2
    assert result.event is not None
    assert result.event.word_id == result.word.id
    assert result.event.review_date.isoformat() == "2026-07-16"


def test_review_reveal_keeps_senses_in_insertion_order_with_reversed_clocks(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    timestamps = iter((NOW, NOW - timedelta(days=1)))
    capture = CaptureService(database, clock=lambda: next(timestamps))
    capture.capture(
        CaptureCommand(
            word="bank",
            operation=CaptureOperation.NEW_WORD,
            card=SenseCard(
                part_of_speech="noun",
                definition="A financial institution.",
                example_sentence="She deposited the cheque at the bank.",
            ),
        )
    )
    capture.capture(
        CaptureCommand(
            word="bank",
            operation=CaptureOperation.NEW_SENSE,
            card=SenseCard(
                part_of_speech="noun",
                definition="Land alongside a river.",
                example_sentence="They rested on the grassy bank.",
            ),
        )
    )
    service.daily_review()

    result = service.complete_review("my answer")

    assert result.status is ReviewCompletionStatus.COMPLETED
    assert [sense.definition for sense in result.word.senses] == [
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
    completed = service.complete_review("brief and direct")
    after_answer = service.daily_review()

    assert second.event == first.event
    assert second.word == first.word
    assert completed.status is ReviewCompletionStatus.COMPLETED
    assert completed.answer_text == "brief and direct"
    assert completed.word is not None
    assert completed.word.last_reviewed == NOW
    assert completed.word.review_status == "reviewed"
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

    assert service.daily_review().word.word == "first"


def test_empty_library_creates_no_event(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)

    result = service.daily_review()

    assert result.status is ReviewPromptStatus.EMPTY
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 0


def test_blank_or_missing_review_does_not_change_state(tmp_path: Path) -> None:
    service, _, _ = setup_service(tmp_path, NOW)

    assert service.complete_review(" ").status is ReviewCompletionStatus.INVALID
    assert service.complete_review("answer").status is ReviewCompletionStatus.NO_PENDING


def test_pending_review_survives_service_restart(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)
    first = service.daily_review()

    restarted = ReviewService(database, ZoneInfo("America/New_York"), lambda: NOW)
    resumed = restarted.daily_review()
    completed = restarted.complete_review("Using few words.")

    assert resumed.event == first.event
    assert resumed.word == first.word
    assert completed.status is ReviewCompletionStatus.COMPLETED
    assert completed.word.word == "laconic"


def test_concurrent_same_day_creation_produces_one_event(tmp_path: Path) -> None:
    service, _, database = setup_service(tmp_path, NOW)
    add_word(database, "laconic", NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.daily_review(), range(2)))

    assert all(result.status is ReviewPromptStatus.PENDING for result in results)
    assert results[0].event == results[1].event
    assert results[0].word == results[1].word
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 1
