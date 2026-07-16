from __future__ import annotations

from .models import (
    CaptureResult,
    CaptureStatus,
    ReviewCompletionResult,
    ReviewCompletionStatus,
    ReviewPromptResult,
    ReviewPromptStatus,
    VocabularySense,
    VocabularyWord,
)


def _display_word(word: str) -> str:
    return word[:1].upper() + word[1:]


def _card(word: VocabularyWord, sense: VocabularySense, footer: str) -> str:
    return (
        f"{_display_word(word.word)} ({sense.part_of_speech})\n\n"
        f"Definition:\n{sense.definition}\n\n"
        f"Example:\n{sense.example_sentence}\n\n"
        f"{footer}"
    )


def format_capture(result: CaptureResult) -> str:
    if result.status is CaptureStatus.INVALID:
        return "Send one word, optionally followed by context on the next line."
    if result.status is CaptureStatus.CONFLICT:
        return "That word changed while I was saving it. Please try again."
    if result.status is CaptureStatus.STORAGE_ERROR:
        return "I couldn't save that word. Please try again."
    if result.word is None or result.sense is None:
        return "I couldn't save that word. Please try again."
    if result.status is CaptureStatus.ALREADY_EXISTS:
        return _card(result.word, result.sense, "Already saved with this meaning.")
    if result.status is CaptureStatus.NEW_SENSE_SAVED:
        return _card(result.word, result.sense, "✓ New meaning saved.")
    return _card(result.word, result.sense, "✓ Saved.")


def format_daily_review(result: ReviewPromptResult) -> str:
    if result.status is ReviewPromptStatus.ALREADY_COMPLETED:
        return ""
    if result.status is ReviewPromptStatus.EMPTY:
        return "Save a word first, then I'll have something to review."
    if result.status is ReviewPromptStatus.STORAGE_ERROR or result.word is None:
        raise RuntimeError("Could not prepare the daily vocabulary review")
    return f"What does '{result.word.word}' mean?"


def format_review_completion(result: ReviewCompletionResult) -> str:
    if result.status is ReviewCompletionStatus.INVALID:
        return "Send an answer, or type 'show answer'."
    if result.status is ReviewCompletionStatus.NO_PENDING:
        return "There isn't a review waiting."
    if result.status is ReviewCompletionStatus.STORAGE_ERROR or result.word is None:
        return "I couldn't record that review. Please try again."
    if len(result.word.senses) == 1:
        sense = result.word.senses[0]
        return (
            f"Definition:\n{sense.definition}\n\n"
            f"Example:\n{sense.example_sentence}"
        )
    return "\n\n".join(
        f"{index}. {sense.part_of_speech} — {sense.definition}\n"
        f"   Example: {sense.example_sentence}"
        for index, sense in enumerate(result.word.senses, start=1)
    )
