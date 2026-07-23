from __future__ import annotations

from datetime import UTC, datetime

from hermes_vocab.formatting import (
    format_capture,
    format_entry,
    format_hint,
    format_review_completion,
    format_test_completion,
    format_test_start,
)
from hermes_vocab.models import (
    CaptureResult,
    CaptureStatus,
    EvaluationGrade,
    ReviewCompletionResult,
    ReviewCompletionStatus,
    TestCompletionResult as CompletionResult,
    TestCompletionStatus as CompletionStatus,
    TestQuestion as Question,
    TestSession as Session,
    TestSessionSnapshot as SessionSnapshot,
    TestSessionStatus as SessionStatus,
    TestStartResult as StartResult,
    TestStartStatus as StartStatus,
    TestSummary as Summary,
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


def test_review_completion_formats_grade_and_feedback_before_single_sense() -> None:
    result = ReviewCompletionResult(
        status=ReviewCompletionStatus.COMPLETED,
        entry=WORD,
        answer_text="It means stubborn.",
        grade=EvaluationGrade.CORRECT,
        feedback="Accurate paraphrase.",
        event_id=7,
    )

    assert format_review_completion(result) == (
        "Grade: Correct\n"
        "Feedback: Accurate paraphrase.\n\n"
        "Definition:\n"
        "Stubbornly refusing to change one's opinion.\n\n"
        "Example:\n"
        "The committee remained obdurate despite new evidence."
    )


def test_review_completion_keeps_multi_sense_reveal_in_database_order() -> None:
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
    result = ReviewCompletionResult(
        status=ReviewCompletionStatus.COMPLETED,
        entry=entry,
        answer_text="Only partly right.",
        grade=EvaluationGrade.PARTIAL,
        feedback="You identified part of the meaning.",
        event_id=7,
    )

    assert format_review_completion(result) == (
        "Grade: Partial\n"
        "Feedback: You identified part of the meaning.\n\n"
        "1. adjective — Stubbornly refusing to change one's opinion.\n"
        "   Example: The committee remained obdurate despite new evidence.\n\n"
        "2. adjective — Resistant to persuasion.\n"
        "   Example: The witness remained obdurate."
    )


def test_review_evaluation_failure_gives_retry_without_revealing_answer() -> None:
    text = format_review_completion(
        ReviewCompletionResult(ReviewCompletionStatus.STORAGE_ERROR)
    )

    assert text == "I couldn't evaluate that answer. Please try again."
    assert "Definition:" not in text
    assert SENSE.definition not in text


def test_test_start_formats_progress_and_exact_current_question() -> None:
    question = Question(
        id=11,
        session_id=7,
        position=3,
        entry=WORD,
        answer_text=None,
        grade=None,
        feedback=None,
        answered_at=None,
    )
    snapshot = SessionSnapshot(
        session=Session(
            id=7,
            status=SessionStatus.ACTIVE,
            started_at=datetime(2026, 7, 19, tzinfo=UTC),
            completed_at=None,
        ),
        questions=(question,),
        current_question=question,
        summary=Summary(correct=1, partial=1),
    )

    assert format_test_start(
        StartResult(StartStatus.RESUMED, snapshot=snapshot)
    ) == (
        "Question 3 of 5\n"
        "What does 'obdurate' mean?"
    )


def test_test_start_formats_insufficient_count_conflict_and_storage_failure() -> None:
    assert format_test_start(
        StartResult(
            StartStatus.INSUFFICIENT_LIBRARY,
            available_count=3,
        )
    ) == "You have 3 saved entries. Save 2 more to start a 5-word test."
    assert format_test_start(
        StartResult(StartStatus.DAILY_REVIEW_PENDING)
    ) == "Finish your daily review before starting a test."
    assert format_test_start(
        StartResult(StartStatus.STORAGE_ERROR)
    ) == "I couldn't start the test. Please try again."


def test_advanced_test_answer_formats_grade_reveal_then_next_progress() -> None:
    answered = Question(
        id=11,
        session_id=7,
        position=1,
        entry=WORD,
        answer_text="It means stubborn.",
        grade=EvaluationGrade.PARTIAL,
        feedback="Right direction.",
        answered_at=datetime(2026, 7, 19, tzinfo=UTC),
    )
    current = Question(
        id=12,
        session_id=7,
        position=2,
        entry=WORD,
        answer_text=None,
        grade=None,
        feedback=None,
        answered_at=None,
    )
    snapshot = SessionSnapshot(
        session=Session(
            id=7,
            status=SessionStatus.ACTIVE,
            started_at=datetime(2026, 7, 19, tzinfo=UTC),
            completed_at=None,
        ),
        questions=(answered, current),
        current_question=current,
        summary=Summary(partial=1),
    )

    assert format_test_completion(
        CompletionResult(
            CompletionStatus.ADVANCED,
            snapshot=snapshot,
            answered_question=answered,
        )
    ) == (
        "Grade: Partial\n"
        "Feedback: Right direction.\n\n"
        "Definition:\n"
        "Stubbornly refusing to change one's opinion.\n\n"
        "Example:\n"
        "The committee remained obdurate despite new evidence.\n\n"
        "Question 2 of 5\n"
        "What does 'obdurate' mean?"
    )


def test_completed_test_answer_formats_reveal_then_category_totals() -> None:
    answered = Question(
        id=15,
        session_id=7,
        position=5,
        entry=WORD,
        answer_text="attempt",
        grade=EvaluationGrade.INCORRECT,
        feedback="That is not the stored meaning.",
        answered_at=datetime(2026, 7, 19, tzinfo=UTC),
    )
    snapshot = SessionSnapshot(
        session=Session(
            id=7,
            status=SessionStatus.COMPLETED,
            started_at=datetime(2026, 7, 19, tzinfo=UTC),
            completed_at=datetime(2026, 7, 19, 0, 5, tzinfo=UTC),
        ),
        questions=(answered,),
        current_question=None,
        summary=Summary(correct=2, partial=2, incorrect=1),
    )

    text = format_test_completion(
        CompletionResult(
            CompletionStatus.COMPLETED,
            snapshot=snapshot,
            answered_question=answered,
        )
    )

    assert text.endswith(
        "Test complete.\n"
        "Results: 2 correct, 2 partial, 1 incorrect."
    )
    assert text.index("Grade: Incorrect") < text.index("Definition:")
    assert text.index("Definition:") < text.index("Test complete.")


def test_test_evaluation_failure_never_reveals() -> None:
    text = format_test_completion(
        CompletionResult(CompletionStatus.STORAGE_ERROR)
    )

    assert text == "I couldn't evaluate that answer. Please try again."
    assert "Definition:" not in text
