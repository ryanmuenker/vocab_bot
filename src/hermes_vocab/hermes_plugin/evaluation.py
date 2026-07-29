from __future__ import annotations

import json
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from hermes_vocab.models import (
    CardDirection,
    Evaluation,
    EvaluationGrade,
    FinalizeResult,
    FinalizeStatus,
    ReviewRating,
    StudyAnswerContext,
    StudyAnswerResult,
    StudyAnswerStatus,
    VocabularyEntry,
)
from hermes_vocab.review import ReviewService

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
    if answer_text.strip().casefold() == "idk":
        return EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(
                EvaluationGrade.INCORRECT,
                "You said you don't know the answer.",
            ),
        )
    return await provider.evaluate(entry, answer_text)


REVERSE_CORRECT_FEEDBACK = "Exact match to the saved entry."
REVERSE_INCORRECT_FEEDBACK = "That does not exactly match the saved entry."


def normalize_reverse_answer(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if character.isalnum())


def allowed_ratings(grade: EvaluationGrade) -> tuple[ReviewRating, ...]:
    if grade is EvaluationGrade.PARTIAL:
        return (ReviewRating.AGAIN, ReviewRating.HARD)
    if grade is EvaluationGrade.CORRECT:
        return (ReviewRating.HARD, ReviewRating.GOOD, ReviewRating.EASY)
    return ()


def parse_rating(
    text: str,
    allowed: tuple[ReviewRating, ...],
) -> ReviewRating | None:
    if not isinstance(text, str):
        return None
    token = " ".join(text.split()).casefold()
    try:
        rating = ReviewRating(token)
    except ValueError:
        return None
    return rating if rating in allowed else None


def _reverse_evaluation(entry: VocabularyEntry, answer_text: str) -> Evaluation:
    if normalize_reverse_answer(answer_text) == normalize_reverse_answer(
        entry.display_text
    ):
        return Evaluation(EvaluationGrade.CORRECT, REVERSE_CORRECT_FEEDBACK)
    return Evaluation(EvaluationGrade.INCORRECT, REVERSE_INCORRECT_FEEDBACK)


def _finalized_result(
    context: StudyAnswerContext,
    choices: tuple[ReviewRating, ...],
    finalization: FinalizeResult,
) -> StudyAnswerResult:
    if finalization.status is FinalizeStatus.COMPLETED:
        status = StudyAnswerStatus.FINALIZED
    elif finalization.status is FinalizeStatus.STORAGE_ERROR:
        status = StudyAnswerStatus.STORAGE_ERROR
    else:
        status = StudyAnswerStatus.STALE
    return StudyAnswerResult(
        status,
        context=context,
        allowed_ratings=choices,
        finalization=finalization,
    )


async def continue_study_answer(
    review_service: ReviewService,
    provider: EvaluationProvider,
    answer_text: str,
) -> StudyAnswerResult:
    context = review_service.current_answer_context()
    if context is None:
        return StudyAnswerResult(StudyAnswerStatus.NO_ACTIVE)
    if context.draft is not None:
        choices = allowed_ratings(context.draft.evaluation.grade)
        if context.draft.evaluation.grade is EvaluationGrade.INCORRECT:
            return _finalized_result(
                context,
                choices,
                review_service.finalize(
                    context.prompt.id,
                    ReviewRating.AGAIN,
                ),
            )
        rating = parse_rating(answer_text, choices)
        if rating is None:
            return StudyAnswerResult(
                StudyAnswerStatus.INVALID_RATING,
                context=context,
                allowed_ratings=choices,
            )
        return _finalized_result(
            context,
            choices,
            review_service.finalize(context.prompt.id, rating),
        )
    if not isinstance(answer_text, str) or not answer_text.strip():
        return StudyAnswerResult(
            StudyAnswerStatus.INVALID_INPUT,
            context=context,
        )

    if answer_text == "show answer" or answer_text.strip().casefold() == "idk":
        evaluated = await evaluate_answer(provider, context.entry, answer_text)
    elif context.queue_item.card.direction is CardDirection.REVERSE:
        evaluated = EvaluationResult(
            EvaluationStatus.VALID,
            _reverse_evaluation(context.entry, answer_text),
        )
    else:
        evaluated = await evaluate_answer(provider, context.entry, answer_text)
    if (
        evaluated.status is not EvaluationStatus.VALID
        or evaluated.evaluation is None
    ):
        return StudyAnswerResult(
            StudyAnswerStatus.EVALUATION_ERROR,
            context=context,
        )
    if (
        review_service.record_answer(
            context.prompt.id,
            answer_text,
            evaluated.evaluation,
        )
        is None
    ):
        return StudyAnswerResult(
            StudyAnswerStatus.STORAGE_ERROR,
            context=context,
        )
    persisted = review_service.current_answer_context()
    if persisted is None or persisted.draft is None:
        return StudyAnswerResult(StudyAnswerStatus.STALE)
    choices = allowed_ratings(persisted.draft.evaluation.grade)
    if persisted.draft.evaluation.grade is EvaluationGrade.INCORRECT:
        return _finalized_result(
            persisted,
            choices,
            review_service.finalize(persisted.prompt.id, ReviewRating.AGAIN),
        )
    return StudyAnswerResult(
        StudyAnswerStatus.AWAITING_RATING,
        context=persisted,
        allowed_ratings=choices,
    )
