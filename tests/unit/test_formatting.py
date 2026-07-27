from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from hermes_vocab.formatting import (
    format_capture,
    format_entry,
    format_hint,
    format_directional_totals,
    format_study_evaluation,
    format_study_evaluation_result,
    format_study_prompt,
    format_study_schedule,
)
from hermes_vocab.models import (
    CardDirection,
    CardScheduleState,
    CaptureResult,
    CaptureStatus,
    EvaluationGrade,
    Evaluation,
    ReviewRating,
    StudyAnswerContext,
    StudyCardSnapshot,
    StudyDraftSnapshot,
    StudyMode,
    StudyProgress,
    StudyPromptSnapshot,
    StudyPromptStatus,
    StudyQueueItemSnapshot,
    StudyQueueStatus,
    StudySessionStatus,
    StudySnapshot,
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


def test_format_hint_returns_complete_stored_example() -> None:
    assert format_hint(WORD) == (
        "Hint: The committee remained obdurate despite new evidence."
    )


def test_format_hint_uses_first_stored_sense_for_multi_sense_entry() -> None:
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
        display_text=WORD.display_text,
        normalized_text=WORD.normalized_text,
        date_added=WORD.date_added,
        last_reviewed=None,
        review_status="new",
        senses=(SENSE, second),
    )

    assert format_hint(entry) == (
        "Hint: The committee remained obdurate despite new evidence."
    )


def study_context(
    *,
    direction: CardDirection = CardDirection.FORWARD,
    entry: VocabularyEntry = WORD,
    sense: VocabularySense | None = None,
    grade: EvaluationGrade | None = None,
    retry: bool = False,
) -> StudyAnswerContext:
    card = StudyCardSnapshot(
        id=101,
        entry_id=entry.id,
        sense_id=sense.id if sense is not None else None,
        direction=direction,
        state=CardScheduleState.NEW,
        stability=None,
        difficulty=None,
        due=datetime(2026, 7, 20, 12, tzinfo=UTC),
        effective_due=datetime(2026, 7, 20, 12, tzinfo=UTC),
        last_review=None,
        repetitions=0,
        lapses=0,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    queue = StudyQueueItemSnapshot(
        id=201,
        card=card,
        position=3 if retry else 1,
        status=StudyQueueStatus.CURRENT,
        retry_of_queue_item_id=99 if retry else None,
    )
    prompt = StudyPromptSnapshot(
        id=301,
        session_id=401,
        queue_item_id=queue.id,
        prompt_key="prompt-1",
        prompt_text="Persisted.",
        status=(
            StudyPromptStatus.ANSWERED
            if grade is not None
            else StudyPromptStatus.DELIVERED
        ),
        prepared_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        delivered_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        answered_at=(
            datetime(2026, 7, 20, 12, 1, tzinfo=UTC)
            if grade is not None
            else None
        ),
    )
    draft = (
        StudyDraftSnapshot(
            id=501,
            submitted_answer="learner answer",
            evaluation=Evaluation(grade, "Right direction."),
            answered_at=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        if grade is not None
        else None
    )
    return StudyAnswerContext(prompt, queue, entry, sense, draft)


def study_snapshot(
    context: StudyAnswerContext,
    *,
    completed: int,
    total: int,
) -> StudySnapshot:
    return StudySnapshot(
        session_id=context.prompt.session_id,
        mode=StudyMode.REVIEW,
        status=StudySessionStatus.ACTIVE,
        local_date=date(2026, 7, 20),
        queue=(context.queue_item,),
        current_prompt=context.prompt,
        progress=StudyProgress(completed, total),
    )


def test_study_prompt_formats_current_total_backlog_and_tail_retry_exactly() -> None:
    current = study_context()
    retry = study_context(retry=True)

    assert format_study_prompt(
        current,
        study_snapshot(current, completed=0, total=3),
        due_backlog=2,
    ) == (
        "Review 1 of 3 · 2 due\n"
        "What does 'obdurate' mean?"
    )
    assert format_study_prompt(
        retry,
        study_snapshot(retry, completed=2, total=3),
        due_backlog=1,
    ) == (
        "Review 3 of 3 · 1 due · retry\n"
        "What does 'obdurate' mean?"
    )


def test_reverse_multi_sense_prompt_contains_only_selected_definition() -> None:
    selected = VocabularySense(
        id=3,
        entry_id=WORD.id,
        definition="Resistant to persuasion.",
        part_of_speech="adjective",
        example_sentence="The witness remained obdurate.",
        source_context=None,
        date_added=WORD.date_added,
    )
    entry = VocabularyEntry(
        id=WORD.id,
        display_text=WORD.display_text,
        normalized_text=WORD.normalized_text,
        date_added=WORD.date_added,
        last_reviewed=None,
        review_status="new",
        senses=(SENSE, selected),
    )
    context = study_context(
        direction=CardDirection.REVERSE,
        entry=entry,
        sense=selected,
    )

    text = format_study_prompt(
        context,
        study_snapshot(context, completed=0, total=5),
        due_backlog=4,
    )

    assert text == (
        "Review 1 of 5 · 4 due\n"
        "Which saved word or expression matches this definition?\n\n"
        "Resistant to persuasion."
    )
    assert "obdurate" not in text.casefold()
    assert selected.example_sentence not in text
    assert SENSE.definition not in text


def test_study_evaluation_reveals_grade_feedback_before_allowed_choices() -> None:
    context = study_context(grade=EvaluationGrade.PARTIAL)

    text = format_study_evaluation(
        context,
        (ReviewRating.AGAIN, ReviewRating.HARD),
    )

    assert text == (
        "Grade: Partial\n"
        "Feedback: Right direction.\n\n"
        "Definition:\n"
        "Stubbornly refusing to change one's opinion.\n\n"
        "Example:\n"
        "The committee remained obdurate despite new evidence.\n\n"
        "Choose effort: Again or Hard."
    )
    assert text.index("Grade:") < text.index("Definition:")
    assert text.index("Definition:") < text.index("Choose effort:")

def test_finalized_incorrect_evaluation_reveals_without_empty_effort_prompt() -> None:
    context = study_context(grade=EvaluationGrade.INCORRECT)

    text = format_study_evaluation_result(context)

    assert text == (
        "Grade: Incorrect\n"
        "Feedback: Right direction.\n\n"
        "Definition:\n"
        "Stubbornly refusing to change one's opinion.\n\n"
        "Example:\n"
        "The committee remained obdurate despite new evidence."
    )
    assert "Choose effort:" not in text



def test_study_schedule_formats_progress_retry_and_next_prompt() -> None:
    next_prompt = (
        "Review 2 of 3 · 1 due\n"
        "What does 'laconic' mean?"
    )

    assert format_study_schedule(
        ReviewRating.AGAIN,
        datetime(2026, 7, 21, 4, tzinfo=UTC),
        StudyProgress(completed=1, total=3),
        retry_queued=True,
        timezone=ZoneInfo("Asia/Kuala_Lumpur"),
        next_prompt=next_prompt,
    ) == (
        "Rated: Again\n"
        "Next due: 2026-07-21 12:00 (UTC+08:00)\n"
        "Progress: 1 of 3 complete.\n"
        "Retry added at the end.\n\n"
        "Review 2 of 3 · 1 due\n"
        "What does 'laconic' mean?"
    )


def test_study_schedule_renders_the_due_instant_in_the_learners_zone() -> None:
    """A bare UTC stamp reads as the wrong time of day to anyone off UTC."""
    due = datetime(2026, 7, 28, 8, 3, tzinfo=UTC)
    progress = StudyProgress(completed=1, total=2)
    for zone, expected in (
        ("UTC", "2026-07-28 08:03 (UTC+00:00)"),
        ("Asia/Kuala_Lumpur", "2026-07-28 16:03 (UTC+08:00)"),
        ("Asia/Kathmandu", "2026-07-28 13:48 (UTC+05:45)"),
        ("America/New_York", "2026-07-28 04:03 (UTC-04:00)"),
    ):
        assert f"Next due: {expected}" in format_study_schedule(
            ReviewRating.GOOD,
            due,
            progress,
            retry_queued=False,
            timezone=ZoneInfo(zone),
        )


def test_final_directional_totals_are_exact() -> None:
    assert format_directional_totals(
        CardDirection.FORWARD,
        correct=2,
        partial=2,
        incorrect=1,
    ) == (
        "Forward test complete.\n"
        "Results: 2 correct, 2 partial, 1 incorrect."
    )
    assert format_directional_totals(
        CardDirection.REVERSE,
        correct=4,
        partial=0,
        incorrect=1,
    ) == (
        "Reverse test complete.\n"
        "Results: 4 correct, 0 partial, 1 incorrect."
    )
