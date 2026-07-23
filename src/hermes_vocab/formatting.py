from __future__ import annotations

from .models import (
    TestCompletionResult,
    TestCompletionStatus,
    TestSessionSnapshot,
    TestStartResult,
    TestStartStatus,
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


def format_hint(entry: VocabularyEntry) -> str:
    return f"Hint: {entry.senses[0].example_sentence}"


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
    if result.status in {
        ReviewPromptStatus.ALREADY_COMPLETED,
        ReviewPromptStatus.TEST_ACTIVE,
    }:
        return ""
    if result.status is ReviewPromptStatus.EMPTY:
        return "Save a word first, then I'll have something to review."
    if result.status is ReviewPromptStatus.STORAGE_ERROR or result.entry is None:
        raise RuntimeError("Could not prepare the daily vocabulary review")
    return f"What does '{result.entry.display_text}' mean?"


def _canonical_reveal(entry: VocabularyEntry) -> str:
    if len(entry.senses) == 1:
        sense = entry.senses[0]
        return (
            f"Definition:\n{sense.definition}\n\n"
            f"Example:\n{sense.example_sentence}"
        )
    return "\n\n".join(
        f"{index}. {sense.part_of_speech} — {sense.definition}\n"
        f"   Example: {sense.example_sentence}"
        for index, sense in enumerate(entry.senses, start=1)
    )


def _test_prompt(snapshot: TestSessionSnapshot) -> str | None:
    question = snapshot.current_question
    if question is None:
        return None
    return (
        f"Question {question.position} of 5\n"
        f"What does '{question.entry.display_text}' mean?"
    )


def format_test_start(result: TestStartResult) -> str:
    if result.status is TestStartStatus.INSUFFICIENT_LIBRARY:
        available = result.available_count or 0
        needed = max(result.required_count - available, 0)
        entry_word = "entry" if available == 1 else "entries"
        return (
            f"You have {available} saved {entry_word}. "
            f"Save {needed} more to start a {result.required_count}-word test."
        )
    if result.status is TestStartStatus.DAILY_REVIEW_PENDING:
        return "Finish your daily review before starting a test."
    if result.status is TestStartStatus.STORAGE_ERROR or result.snapshot is None:
        return "I couldn't start the test. Please try again."
    prompt = _test_prompt(result.snapshot)
    if prompt is None:
        return "I couldn't start the test. Please try again."
    return prompt


def format_test_completion(result: TestCompletionResult) -> str:
    if result.status is TestCompletionStatus.INVALID:
        return "Send an answer, or type 'show answer'."
    if result.status is TestCompletionStatus.NO_ACTIVE:
        return "There isn't an active test."
    if result.status is TestCompletionStatus.STALE:
        if result.snapshot is None:
            return "That answer was already recorded."
        prompt = _test_prompt(result.snapshot)
        if prompt is None:
            return "There isn't an active test."
        return f"That answer was already recorded.\n\n{prompt}"
    if (
        result.status is TestCompletionStatus.STORAGE_ERROR
        or result.snapshot is None
        or result.answered_question is None
        or result.answered_question.grade is None
        or result.answered_question.feedback is None
        or not result.answered_question.feedback.strip()
    ):
        return "I couldn't evaluate that answer. Please try again."

    answered = result.answered_question
    text = (
        f"Grade: {answered.grade.value.title()}\n"
        f"Feedback: {answered.feedback}\n\n"
        f"{_canonical_reveal(answered.entry)}"
    )
    if result.status is TestCompletionStatus.ADVANCED:
        prompt = _test_prompt(result.snapshot)
        if prompt is None:
            return "I couldn't evaluate that answer. Please try again."
        return f"{text}\n\n{prompt}"
    if result.status is TestCompletionStatus.COMPLETED:
        summary = result.snapshot.summary
        return (
            f"{text}\n\n"
            "Test complete.\n"
            f"Results: {summary.correct} correct, {summary.partial} partial, "
            f"{summary.incorrect} incorrect."
        )
    return "I couldn't evaluate that answer. Please try again."


def format_review_completion(result: ReviewCompletionResult) -> str:
    if result.status is ReviewCompletionStatus.INVALID:
        return "Send an answer, or type 'show answer'."
    if result.status is ReviewCompletionStatus.NO_PENDING:
        return "There isn't a review waiting."
    if (
        result.status is ReviewCompletionStatus.STORAGE_ERROR
        or result.entry is None
        or result.grade is None
        or result.feedback is None
        or not result.feedback.strip()
    ):
        return "I couldn't evaluate that answer. Please try again."
    evaluation = (
        f"Grade: {result.grade.value.title()}\n"
        f"Feedback: {result.feedback}"
    )
    reveal = _canonical_reveal(result.entry)
    return f"{evaluation}\n\n{reveal}"
