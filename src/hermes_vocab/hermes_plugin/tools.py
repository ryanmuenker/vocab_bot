from __future__ import annotations

import json

from hermes_vocab.capture import CaptureService
from hermes_vocab.formatting import format_capture, format_review_completion
from hermes_vocab.models import (
    CaptureCommand,
    CaptureOperation,
    CaptureResult,
    CaptureStatus,
    ReviewCompletionStatus,
    SenseCard,
)
from hermes_vocab.review import ReviewService


_CARD_FIELDS = ("part_of_speech", "definition", "example_sentence")


def _capture_command(args: object) -> CaptureCommand | None:
    if not isinstance(args, dict):
        return None
    word = args.get("word")
    operation_value = args.get("operation")
    if not isinstance(word, str) or not isinstance(operation_value, str):
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
        word=word,
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
            "word_exists": result.word is not None,
            "senses": [
                {
                    "id": sense.id,
                    "part_of_speech": sense.part_of_speech,
                    "definition": sense.definition,
                }
                for sense in result.word.senses
            ]
            if result.word is not None
            else [],
        }
    return payload


class ToolHandlers:
    def __init__(
        self,
        capture_service: CaptureService,
        review_service: ReviewService,
    ) -> None:
        self.capture_service = capture_service
        self.review_service = review_service

    def save_card(self, args: dict, **kwargs) -> str:
        command = _capture_command(args)
        result = (
            self.capture_service.capture(command)
            if command is not None
            else CaptureResult(CaptureStatus.INVALID)
        )
        return json.dumps(_capture_payload(result), ensure_ascii=False)

    def complete_review(self, args: dict, **kwargs) -> str:
        try:
            result = self.review_service.complete_review(args.get("answer_text", ""))
            return json.dumps(
                {
                    "status": result.status.value,
                    "text": format_review_completion(result),
                },
                ensure_ascii=False,
            )
        except Exception:
            return json.dumps(
                {
                    "status": ReviewCompletionStatus.STORAGE_ERROR.value,
                    "text": "I couldn't record that review. Please try again.",
                }
            )
