from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from hermes_vocab.models import (
    Evaluation,
    EvaluationGrade,
    PendingReviewStatus,
    ReviewCompletionResult,
    ReviewCompletionStatus,
    VocabularyEntry,
    TestCompletionResult,
    TestCompletionStatus,
    TestSnapshotStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService

MAX_EVALUATION_FEEDBACK_LENGTH = 500
SHOW_ANSWER_FEEDBACK = "You chose to reveal the answer."


class EvaluationStatus(StrEnum):
    VALID = "valid"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    status: EvaluationStatus
    evaluation: Evaluation | None = None


def parse_evaluation_response(text: str) -> EvaluationResult:
    if not isinstance(text, str):
        return EvaluationResult(EvaluationStatus.INVALID_RESPONSE)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return EvaluationResult(EvaluationStatus.INVALID_RESPONSE)
    if not isinstance(payload, dict) or set(payload) != {"grade", "feedback"}:
        return EvaluationResult(EvaluationStatus.INVALID_RESPONSE)

    grade_value = payload["grade"]
    feedback_value = payload["feedback"]
    if not isinstance(grade_value, str) or not isinstance(feedback_value, str):
        return EvaluationResult(EvaluationStatus.INVALID_RESPONSE)
    try:
        grade = EvaluationGrade(grade_value)
    except ValueError:
        return EvaluationResult(EvaluationStatus.INVALID_RESPONSE)
    feedback = feedback_value.strip()
    if not 0 < len(feedback) <= MAX_EVALUATION_FEEDBACK_LENGTH:
        return EvaluationResult(EvaluationStatus.INVALID_RESPONSE)
    return EvaluationResult(
        EvaluationStatus.VALID,
        Evaluation(grade=grade, feedback=feedback),
    )


_SYSTEM_PROMPT = (
    "You evaluate an English vocabulary learner's answer against stored senses. "
    "Return JSON only with exactly two top-level keys: grade and feedback. "
    "Grade must be exactly correct, partial, or incorrect. Accept an accurate "
    "semantic paraphrase as correct even when it shares no wording with the stored "
    "definition. A response matching any one valid stored sense can be correct; do "
    "not require the learner to enumerate every sense. Use partial for an incomplete "
    "but directionally valid meaning and incorrect for an unrelated or wrong meaning. "
    "Feedback must briefly explain the grade, must not be blank, and must be at most "
    f"{MAX_EVALUATION_FEEDBACK_LENGTH} characters."
)


class EvaluationProvider:
    def __init__(self, call_llm: Callable[..., Awaitable[str]]) -> None:
        self._call_llm = call_llm

    async def evaluate(
        self,
        entry: VocabularyEntry,
        answer_text: str,
    ) -> EvaluationResult:
        if not isinstance(answer_text, str) or not answer_text.strip():
            return EvaluationResult(EvaluationStatus.INVALID_RESPONSE)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "display_text": entry.display_text,
                        "answer_text": answer_text,
                        "senses": [
                            {
                                "part_of_speech": sense.part_of_speech,
                                "definition": sense.definition,
                                "example_sentence": sense.example_sentence,
                            }
                            for sense in entry.senses
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            response = await self._call_llm(
                task="vocabulary_answer_evaluation",
                messages=messages,
                max_tokens=500,
                temperature=0,
                tools=[],
            )
        except Exception:
            return EvaluationResult(EvaluationStatus.PROVIDER_ERROR)
        return parse_evaluation_response(response)


async def evaluate_answer(
    provider: EvaluationProvider,
    entry: VocabularyEntry,
    answer_text: str,
) -> EvaluationResult:
    if answer_text == "show answer":
        return EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(
                EvaluationGrade.INCORRECT,
                SHOW_ANSWER_FEEDBACK,
            ),
        )
    return await provider.evaluate(entry, answer_text)


async def complete_pending_review(
    review_service: ReviewService,
    provider: EvaluationProvider,
    answer_text: str,
) -> ReviewCompletionResult:
    if not isinstance(answer_text, str) or not answer_text.strip():
        return ReviewCompletionResult(ReviewCompletionStatus.INVALID)

    try:
        prepared = review_service.pending_review()
    except Exception:
        return ReviewCompletionResult(ReviewCompletionStatus.STORAGE_ERROR)

    if prepared.status is PendingReviewStatus.STORAGE_ERROR:
        return ReviewCompletionResult(ReviewCompletionStatus.STORAGE_ERROR)
    if (
        prepared.status is not PendingReviewStatus.PENDING
        or prepared.event is None
        or prepared.entry is None
    ):
        return ReviewCompletionResult(ReviewCompletionStatus.NO_PENDING)

    evaluated = await evaluate_answer(provider, prepared.entry, answer_text)
    if (
        evaluated.status is not EvaluationStatus.VALID
        or evaluated.evaluation is None
    ):
        return ReviewCompletionResult(ReviewCompletionStatus.STORAGE_ERROR)
    evaluation = evaluated.evaluation

    try:
        return review_service.complete_review(
            prepared.event.id,
            answer_text,
            evaluation,
        )
    except Exception:
        return ReviewCompletionResult(ReviewCompletionStatus.STORAGE_ERROR)


async def complete_test_question(
    test_service: TestSessionService,
    provider: EvaluationProvider,
    answer_text: str,
) -> TestCompletionResult:
    if not isinstance(answer_text, str) or not answer_text.strip():
        return TestCompletionResult(TestCompletionStatus.INVALID)

    try:
        prepared = test_service.current()
    except Exception:
        return TestCompletionResult(TestCompletionStatus.STORAGE_ERROR)
    if prepared.status is TestSnapshotStatus.STORAGE_ERROR:
        return TestCompletionResult(TestCompletionStatus.STORAGE_ERROR)
    if (
        prepared.status is not TestSnapshotStatus.ACTIVE
        or prepared.snapshot is None
        or prepared.snapshot.current_question is None
    ):
        return TestCompletionResult(TestCompletionStatus.NO_ACTIVE)

    question = prepared.snapshot.current_question
    evaluated = await evaluate_answer(provider, question.entry, answer_text)
    if (
        evaluated.status is not EvaluationStatus.VALID
        or evaluated.evaluation is None
    ):
        return TestCompletionResult(TestCompletionStatus.STORAGE_ERROR)

    try:
        return test_service.complete(
            question.id,
            answer_text,
            evaluated.evaluation,
        )
    except Exception:
        return TestCompletionResult(TestCompletionStatus.STORAGE_ERROR)
