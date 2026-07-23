from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from hermes_vocab.capture import CaptureService
from hermes_vocab.database import Database
from hermes_vocab.formatting import format_daily_review, format_review_completion
from hermes_vocab.models import (
    CaptureCommand,
    CaptureOperation,
    EvaluationGrade,
    ReviewCompletionResult,
    ReviewCompletionStatus,
    ReviewEvent,
    ReviewPromptResult,
    ReviewPromptStatus,
    SenseCard,
    VocabularySense,
    VocabularyEntry,
)
from hermes_vocab.test_session import TestSessionService as VocabularyTestSessionService


FIRST_SENSE = VocabularySense(
    id=1,
    entry_id=1,
    definition="Using very few words.",
    part_of_speech="adjective",
    example_sentence="His laconic reply ended the discussion.",
    source_context=None,
    date_added=datetime(2026, 7, 1, tzinfo=UTC),
)
WORD = VocabularyEntry(
    id=1,
    display_text="laconic",
    normalized_text="laconic",
    date_added=datetime(2026, 7, 1, tzinfo=UTC),
    last_reviewed=None,
    review_status="new",
    senses=(FIRST_SENSE,),
)
EVENT = ReviewEvent(
    id=1,
    entry_id=1,
    review_date=date(2026, 7, 16),
    status="pending",
    prompted_at=datetime(2026, 7, 16, 12, tzinfo=UTC),
    answered_at=None,
    answer_text=None,
)


def test_pending_review_outputs_exact_question() -> None:
    assert format_daily_review(
        ReviewPromptResult(ReviewPromptStatus.PENDING, EVENT, WORD)
    ) == "What does 'laconic' mean?"


def test_answered_same_day_outputs_nothing() -> None:
    assert format_daily_review(
        ReviewPromptResult(ReviewPromptStatus.ALREADY_COMPLETED, EVENT, WORD)
    ) == ""


def test_active_test_outputs_nothing() -> None:
    assert format_daily_review(
        ReviewPromptResult(ReviewPromptStatus.TEST_ACTIVE)
    ) == ""


def test_empty_library_outputs_capture_first_guidance() -> None:
    assert format_daily_review(ReviewPromptResult(ReviewPromptStatus.EMPTY)) == (
        "Save a word first, then I'll have something to review."
    )


def test_one_sense_completion_is_grade_first_before_canonical_reveal() -> None:
    text = format_review_completion(
        ReviewCompletionResult(
            ReviewCompletionStatus.COMPLETED,
            entry=WORD,
            answer_text="show answer",
            grade=EvaluationGrade.INCORRECT,
            feedback="You chose to reveal the answer.",
        )
    )

    assert text == (
        "Grade: Incorrect\n"
        "Feedback: You chose to reveal the answer.\n\n"
        "Definition:\nUsing very few words.\n\n"
        "Example:\nHis laconic reply ended the discussion."
    )


def test_multi_sense_completion_numbers_every_sense_in_capture_order() -> None:
    second_sense = VocabularySense(
        id=2,
        entry_id=1,
        definition="Land alongside a river.",
        part_of_speech="noun",
        example_sentence="They rested on the grassy bank.",
        source_context="They rested beside the river.",
        date_added=datetime(2026, 7, 2, tzinfo=UTC),
    )
    bank = VocabularyEntry(
        id=1,
        display_text="bank",
        normalized_text="bank",
        date_added=datetime(2026, 7, 1, tzinfo=UTC),
        last_reviewed=None,
        review_status="new",
        senses=(
            VocabularySense(
                id=1,
                entry_id=1,
                definition="A financial institution.",
                part_of_speech="noun",
                example_sentence="She deposited the cheque at the bank.",
                source_context=None,
                date_added=datetime(2026, 7, 1, tzinfo=UTC),
            ),
            second_sense,
        ),
    )

    text = format_review_completion(
        ReviewCompletionResult(
            ReviewCompletionStatus.COMPLETED,
            entry=bank,
            answer_text="a partly correct answer",
            grade=EvaluationGrade.PARTIAL,
            feedback="You identified only one sense.",
        )
    )

    assert text == (
        "Grade: Partial\n"
        "Feedback: You identified only one sense.\n\n"
        "1. noun — A financial institution.\n"
        "   Example: She deposited the cheque at the bank.\n\n"
        "2. noun — Land alongside a river.\n"
        "   Example: They rested on the grassy bank."
    )


def test_daily_script_prints_question_from_configured_database(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = Database(path)
    database.initialize()
    CaptureService(database).capture(
        CaptureCommand(
            display_text="laconic",
            operation=CaptureOperation.NEW_ENTRY,
            card=SenseCard(
                part_of_speech="adjective",
                definition="Using very few words.",
                example_sentence="His laconic reply ended the discussion.",
            ),
        )
    )
    environment = {
        **os.environ,
        "HERMES_VOCAB_DB": str(path),
        "HERMES_TIMEZONE": "UTC",
    }

    completed = subprocess.run(
        [sys.executable, "scripts/daily_review.py"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0
    assert completed.stdout == "What does 'laconic' mean?\n"
    assert completed.stderr == ""

    repeated = subprocess.run(
        [sys.executable, "scripts/daily_review.py"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert repeated.returncode == 0
    assert repeated.stdout == "What does 'laconic' mean?\n"
    assert repeated.stderr == ""
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_events"
        ).fetchone()[0] == 1


def test_daily_script_is_silent_and_creates_no_event_during_active_test(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = Database(path)
    database.initialize()
    capture = CaptureService(database)
    for index in range(5):
        capture.capture(
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
    started = VocabularyTestSessionService(database).start()

    completed = subprocess.run(
        [sys.executable, "scripts/daily_review.py"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HERMES_VOCAB_DB": str(path),
            "HERMES_TIMEZONE": "UTC",
        },
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_events"
        ).fetchone()[0] == 0
