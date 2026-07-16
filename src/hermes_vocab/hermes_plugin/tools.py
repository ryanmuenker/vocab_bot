from __future__ import annotations

import json

from hermes_vocab.capture import CaptureService
from hermes_vocab.formatting import format_capture, format_review_completion
from hermes_vocab.models import CaptureStatus, EntryCard, ReviewCompletionStatus
from hermes_vocab.review import ReviewService


class ToolHandlers:
    def __init__(
        self,
        capture_service: CaptureService,
        review_service: ReviewService,
    ) -> None:
        self.capture_service = capture_service
        self.review_service = review_service

    def save_card(self, args: dict, **kwargs) -> str:
        try:
            result = self.capture_service.capture(
                EntryCard(
                    word=args.get("word", ""),
                    part_of_speech=args.get("part_of_speech", ""),
                    definition=args.get("definition", ""),
                    example_sentence=args.get("example_sentence", ""),
                )
            )
            return json.dumps(
                {"status": result.status.value, "text": format_capture(result)},
                ensure_ascii=False,
            )
        except Exception:
            result = self.capture_service.capture(EntryCard("", "", "", ""))
            return json.dumps(
                {
                    "status": CaptureStatus.INVALID.value,
                    "text": format_capture(result),
                },
                ensure_ascii=False,
            )

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
