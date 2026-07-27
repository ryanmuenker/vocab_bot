from __future__ import annotations

import json

from hermes_vocab.capture import CaptureService
from hermes_vocab.formatting import (
    format_capture,
    format_directional_totals,
    format_study_evaluation,
    format_study_evaluation_result,
    format_study_schedule,
)
from hermes_vocab.models import (
    CaptureCommand,
    CaptureOperation,
    CaptureResult,
    CaptureStatus,
    CardDirection,
    EvaluationGrade,
    SenseCard,
    StudyAnswerResult,
    StudyAnswerStatus,
    StudyMode,
    StudySessionStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService
from .evaluation import EvaluationProvider, continue_study_answer


_CARD_FIELDS = ("part_of_speech", "definition", "example_sentence")


def _capture_command(args: object) -> CaptureCommand | None:
    if not isinstance(args, dict):
        return None
    display_text = args.get("display_text")
    operation_value = args.get("operation")
    if not isinstance(display_text, str) or not isinstance(operation_value, str):
        return None
    try:
        operation = CaptureOperation(operation_value)
    except ValueError:
        return None

    source_context = args.get("source_context")
    if "source_context" in args and not isinstance(source_context, str):
        return None
    matching_sense_id = args.get("matching_sense_id")
    if "matching_sense_id" in args and type(matching_sense_id) is not int:
        return None

    card_values = [args.get(field) for field in _CARD_FIELDS]
    if any(field in args for field in _CARD_FIELDS):
        if not all(isinstance(value, str) for value in card_values):
            return None
        card = SenseCard(*card_values)
    else:
        card = None
    return CaptureCommand(
        display_text=display_text,
        operation=operation,
        card=card,
        source_context=source_context,
        matching_sense_id=matching_sense_id,
    )


def _capture_payload(result: CaptureResult) -> dict:
    payload = {
        "status": result.status.value,
        "text": format_capture(result),
    }
    if result.status is CaptureStatus.CONFLICT:
        payload["state"] = {
            "entry_exists": result.entry is not None,
            "senses": [
                {
                    "id": sense.id,
                    "part_of_speech": sense.part_of_speech,
                    "definition": sense.definition,
                }
                for sense in result.entry.senses
            ]
            if result.entry is not None
            else [],
        }
    return payload


class ToolHandlers:
    def __init__(
        self,
        capture_service: CaptureService,
        review_service: ReviewService,
        evaluation_provider: EvaluationProvider,
        test_service: TestSessionService,
    ) -> None:
        self.capture_service = capture_service
        self.review_service = review_service
        self.evaluation_provider = evaluation_provider
        self.test_service = test_service

    def save_card(self, args: dict, **kwargs) -> str:
        command = _capture_command(args)
        result = (
            self.capture_service.capture(command)
            if command is not None
            else CaptureResult(CaptureStatus.INVALID)
        )
        return json.dumps(_capture_payload(result), ensure_ascii=False)

    async def continue_study(self, args: dict, **kwargs) -> str:
        try:
            answer_text = args.get("answer_text", "")
            result = await continue_study_answer(
                self.review_service,
                self.evaluation_provider,
                answer_text,
            )
            return json.dumps(
                self._study_payload(result),
                ensure_ascii=False,
            )
        except Exception:
            return json.dumps(
                {
                    "status": StudyAnswerStatus.STORAGE_ERROR.value,
                    "text": "I couldn't save that study step. Please try again.",
                }
            )

    def _study_payload(self, result: StudyAnswerResult) -> dict:
        payload: dict[str, object] = {
            "status": result.status.value,
            "allowed_ratings": [
                rating.value for rating in result.allowed_ratings
            ],
        }
        if (
            result.status is StudyAnswerStatus.AWAITING_RATING
            and result.context is not None
        ):
            payload["text"] = format_study_evaluation(
                result.context,
                result.allowed_ratings,
            )
            return payload
        if (
            result.status is StudyAnswerStatus.FINALIZED
            and result.finalization is not None
            and result.finalization.transition is not None
            and result.finalization.snapshot is not None
        ):
            snapshot = result.finalization.snapshot
            if (
                snapshot.status is StudySessionStatus.ACTIVE
                and snapshot.mode is not StudyMode.REVIEW
            ):
                prepared = self.test_service.prepare_current_prompt()
                if prepared is not None:
                    snapshot = prepared
            if (
                snapshot.status is StudySessionStatus.COMPLETED
                and snapshot.mode is not StudyMode.REVIEW
            ):
                totals = self.test_service.summary(snapshot.session_id)
                if totals is not None:
                    direction = (
                        CardDirection.FORWARD
                        if snapshot.mode is StudyMode.TEST_FORWARD
                        else CardDirection.REVERSE
                    )
                    totals_text = format_directional_totals(
                        direction,
                        correct=totals.correct,
                        partial=totals.partial,
                        incorrect=totals.incorrect,
                    )
                    payload["text"] = self._with_finalized_evaluation(
                        result,
                        totals_text,
                    )
                    return payload
            transition = result.finalization.transition
            schedule_text = format_study_schedule(
                result.finalization.transition.rating,
                transition.effective_due,
                snapshot.progress,
                retry_queued=transition.retry_same_session,
                next_prompt=(
                    snapshot.current_prompt.prompt_text
                    if (
                        snapshot.mode is not StudyMode.REVIEW
                        and snapshot.current_prompt is not None
                    )
                    else None
                ),
            )
            payload["text"] = self._with_finalized_evaluation(
                result,
                schedule_text,
            )
            return payload
        messages = {
            StudyAnswerStatus.INVALID_INPUT: "Send a non-empty answer.",
            StudyAnswerStatus.INVALID_RATING: "Send one of the listed effort ratings.",
            StudyAnswerStatus.EVALUATION_ERROR: "I couldn't evaluate that answer. Please try again.",
            StudyAnswerStatus.NO_ACTIVE: "There isn't a delivered study prompt waiting.",
            StudyAnswerStatus.STALE: "That study prompt is no longer current.",
            StudyAnswerStatus.STORAGE_ERROR: "I couldn't save that study step. Please try again.",
        }
        payload["text"] = messages.get(
            result.status,
            "I couldn't continue that study step.",
        )
        return payload

    @staticmethod
    def _with_finalized_evaluation(
        result: StudyAnswerResult,
        continuation: str,
    ) -> str:
        if (
            result.context is not None
            and result.context.draft is not None
            and result.context.draft.evaluation.grade is EvaluationGrade.INCORRECT
        ):
            evaluation = format_study_evaluation_result(result.context)
            return f"{evaluation}\n\n{continuation}"
        return continuation
