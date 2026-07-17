from __future__ import annotations

from datetime import UTC, datetime

from hermes_vocab.formatting import format_capture
from hermes_vocab.models import (
    CaptureResult,
    CaptureStatus,
    VocabularySense,
    VocabularyEntry,
)


SENSE = VocabularySense(
    id=2,
    entry_id=1,
    definition="Stubbornly refusing to change one's opinion.",
    part_of_speech="adjective",
    example_sentence="The committee remained obdurate despite new evidence.",
    source_context="The committee stayed obdurate.",
    date_added=datetime(2026, 7, 16, tzinfo=UTC),
)
WORD = VocabularyEntry(
    id=1,
    display_text="obdurate",
    normalized_text="obdurate",
    date_added=datetime(2026, 7, 16, tzinfo=UTC),
    last_reviewed=None,
    review_status="new",
    senses=(SENSE,),
)


def test_saved_card_has_exact_telegram_shape() -> None:
    text = format_capture(CaptureResult(CaptureStatus.SAVED, WORD, SENSE))

    assert text == (
        "Obdurate (adjective)\n\n"
        "Definition:\n"
        "Stubbornly refusing to change one's opinion.\n\n"
        "Example:\n"
        "The committee remained obdurate despite new evidence.\n\n"
        "✓ Saved."
    )


def test_capture_statuses_have_distinct_deterministic_messages() -> None:
    assert format_capture(
        CaptureResult(CaptureStatus.NEW_SENSE_SAVED, WORD, SENSE)
    ).endswith("✓ New meaning saved.")
    assert format_capture(
        CaptureResult(CaptureStatus.ALREADY_EXISTS, WORD, SENSE)
    ).endswith("Already saved with this meaning.")
    conflict = format_capture(CaptureResult(CaptureStatus.CONFLICT, WORD))
    assert conflict == "That entry changed while I was saving it. Please try again."
    assert "Saved" not in conflict


def test_invalid_and_storage_failures_are_concise() -> None:
    assert format_capture(CaptureResult(CaptureStatus.INVALID)) == (
        "Send a word or expression, optionally followed by context on the next line."
    )
    assert format_capture(CaptureResult(CaptureStatus.STORAGE_ERROR)) == (
        "I couldn't save that entry. Please try again."
    )
