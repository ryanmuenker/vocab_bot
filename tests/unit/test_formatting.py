from __future__ import annotations

from datetime import UTC, datetime

from hermes_vocab.formatting import format_capture, format_entry
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


def test_format_entry_preserves_one_sense_shape_with_requested_footer() -> None:
    assert format_entry(WORD, "Already saved.") == (
        "obdurate (adjective)\n\n"
        "Definition:\n"
        "Stubbornly refusing to change one's opinion.\n\n"
        "Example:\n"
        "The committee remained obdurate despite new evidence.\n\n"
        "Already saved."
    )


def test_format_entry_preserves_all_senses_in_database_order() -> None:
    second = VocabularySense(
        id=3,
        entry_id=1,
        definition="Resistant to persuasion.",
        part_of_speech="adjective",
        example_sentence="The witness remained obdurate.",
        source_context=None,
        date_added=datetime(2026, 7, 16, tzinfo=UTC),
    )
    entry = VocabularyEntry(
        id=WORD.id,
        display_text="pro forma",
        normalized_text="pro forma",
        date_added=WORD.date_added,
        last_reviewed=None,
        review_status="new",
        senses=(SENSE, second),
    )

    assert format_entry(entry, "✓ Saved.") == (
        "pro forma\n\n"
        "1. adjective\n"
        "Definition:\n"
        "Stubbornly refusing to change one's opinion.\n"
        "Example:\n"
        "The committee remained obdurate despite new evidence.\n\n"
        "2. adjective\n"
        "Definition:\n"
        "Resistant to persuasion.\n"
        "Example:\n"
        "The witness remained obdurate.\n\n"
        "✓ Saved."
    )
