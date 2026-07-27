from __future__ import annotations

from datetime import UTC, datetime

from .models import (
    CardDirection,
    CaptureResult,
    CaptureStatus,
    ReviewRating,
    StudyAnswerContext,
    StudyProgress,
    StudySnapshot,
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


def format_study_prompt(
    context: StudyAnswerContext,
    snapshot: StudySnapshot,
    *,
    due_backlog: int,
) -> str:
    current = min(snapshot.progress.completed + 1, snapshot.progress.total)
    retry = (
        " · retry"
        if context.queue_item.retry_of_queue_item_id is not None
        else ""
    )
    header = (
        f"Review {current} of {snapshot.progress.total} · "
        f"{due_backlog} due{retry}"
    )
    if context.queue_item.card.direction is CardDirection.REVERSE:
        if context.sense is None:
            raise ValueError("reverse study prompts require a selected sense")
        question = (
            "Which saved word or expression matches this definition?\n\n"
            f"{context.sense.definition}"
        )
    else:
        question = f"What does '{context.entry.display_text}' mean?"
    return f"{header}\n{question}"


def format_study_evaluation_result(context: StudyAnswerContext) -> str:
    if context.draft is None:
        raise ValueError("study evaluation formatting requires a persisted draft")
    evaluation = context.draft.evaluation
    if context.queue_item.card.direction is CardDirection.REVERSE:
        if context.sense is None:
            raise ValueError("reverse evaluation requires a selected sense")
        reveal = (
            f"Answer: {context.entry.display_text}\n\n"
            f"Definition:\n{context.sense.definition}\n\n"
            f"Example:\n{context.sense.example_sentence}"
        )
    else:
        reveal = _canonical_reveal(context.entry)
    return (
        f"Grade: {evaluation.grade.value.title()}\n"
        f"Feedback: {evaluation.feedback}\n\n"
        f"{reveal}"
    )


def format_study_evaluation(
    context: StudyAnswerContext,
    choices: tuple[ReviewRating, ...],
) -> str:
    result = format_study_evaluation_result(context)
    choice_text = " or ".join(choice.value.title() for choice in choices)
    return f"{result}\n\nChoose effort: {choice_text}."


def format_study_schedule(
    rating: ReviewRating,
    effective_due: datetime,
    progress: StudyProgress,
    *,
    retry_queued: bool,
    next_prompt: str | None = None,
) -> str:
    due = effective_due.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"Rated: {rating.value.title()}",
        f"Next due: {due}",
        f"Progress: {progress.completed} of {progress.total} complete.",
    ]
    if retry_queued:
        lines.append("Retry added at the end.")
    text = "\n".join(lines)
    return f"{text}\n\n{next_prompt}" if next_prompt else text


def format_directional_totals(
    direction: CardDirection,
    *,
    correct: int,
    partial: int,
    incorrect: int,
) -> str:
    return (
        f"{direction.value.title()} test complete.\n"
        f"Results: {correct} correct, {partial} partial, "
        f"{incorrect} incorrect."
    )
