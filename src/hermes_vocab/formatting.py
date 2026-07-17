from __future__ import annotations

from .models import (
    CaptureResult,
    CaptureStatus,
    ReviewCompletionResult,
    ReviewCompletionStatus,
    ReviewPromptResult,
    ReviewPromptStatus,
    VocabularyEntry,
    VocabularySense,
)


def _display_entry(text: str) -> str:
    return text[:1].upper() + text[1:]


def _card(entry: VocabularyEntry, sense: VocabularySense, footer: str) -> str:
    return (
        f"{_display_entry(entry.display_text)} ({sense.part_of_speech})\n\n"
        f"Definition:\n{sense.definition}\n\n"
        f"Example:\n{sense.example_sentence}\n\n"
        f"{footer}"
    )


def format_entry(entry: VocabularyEntry, footer: str) -> str:
    if len(entry.senses) == 1:
        sense = entry.senses[0]
        return (
            f"{entry.display_text} ({sense.part_of_speech})\n\n"
            f"Definition:\n{sense.definition}\n\n"
            f"Example:\n{sense.example_sentence}\n\n"
            f"{footer}"
        )
    senses = "\n\n".join(
        f"{index}. {sense.part_of_speech}\n"
        f"Definition:\n{sense.definition}\n"
        f"Example:\n{sense.example_sentence}"
        for index, sense in enumerate(entry.senses, start=1)
    )
    return f"{entry.display_text}\n\n{senses}\n\n{footer}"


def format_capture(result: CaptureResult) -> str:
    if result.status is CaptureStatus.INVALID:
        return "Send a word or expression, optionally followed by context on the next line."
    if result.status is CaptureStatus.CONFLICT:
        return "That entry changed while I was saving it. Please try again."
    if result.status is CaptureStatus.STORAGE_ERROR:
        return "I couldn't save that entry. Please try again."
    if result.entry is None or result.sense is None:
        return "I couldn't save that entry. Please try again."
    if result.status is CaptureStatus.ALREADY_EXISTS:
        return _card(result.entry, result.sense, "Already saved with this meaning.")
    if result.status is CaptureStatus.NEW_SENSE_SAVED:
        return _card(result.entry, result.sense, "✓ New meaning saved.")
    return _card(result.entry, result.sense, "✓ Saved.")


def format_daily_review(result: ReviewPromptResult) -> str:
    if result.status is ReviewPromptStatus.ALREADY_COMPLETED:
        return ""
    if result.status is ReviewPromptStatus.EMPTY:
        return "Save a word first, then I'll have something to review."
    if result.status is ReviewPromptStatus.STORAGE_ERROR or result.entry is None:
        raise RuntimeError("Could not prepare the daily vocabulary review")
    return f"What does '{result.entry.display_text}' mean?"


def format_review_completion(result: ReviewCompletionResult) -> str:
    if result.status is ReviewCompletionStatus.INVALID:
        return "Send an answer, or type 'show answer'."
    if result.status is ReviewCompletionStatus.NO_PENDING:
        return "There isn't a review waiting."
    if result.status is ReviewCompletionStatus.STORAGE_ERROR or result.entry is None:
        return "I couldn't record that review. Please try again."
    if len(result.entry.senses) == 1:
        sense = result.entry.senses[0]
        return (
            f"Definition:\n{sense.definition}\n\n"
            f"Example:\n{sense.example_sentence}"
        )
    return "\n\n".join(
        f"{index}. {sense.part_of_speech} — {sense.definition}\n"
        f"   Example: {sense.example_sentence}"
        for index, sense in enumerate(result.entry.senses, start=1)
    )
