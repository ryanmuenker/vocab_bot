from __future__ import annotations

import asyncio
import sqlite3

from hermes_vocab.capture import CaptureService, normalize_entry_text
from hermes_vocab.formatting import (
    format_entry,
    format_hint,
    format_review_completion,
    format_test_completion,
)
from hermes_vocab.models import (
    CaptureStatus,
    EntryTextStatus,
    PendingReviewStatus,
    TestSnapshotStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService

from .definition import DefinitionProvider, DefinitionStatus
from .evaluation import (
    EvaluationProvider,
    complete_pending_review,
    complete_test_question,
)

_EMPTY = "Send a word or phrase."
_TOO_LONG = "Send a word or phrase under 500 characters."
_NOT_FOUND = "I couldn't define that. Please try another word or phrase."
_DEFINITION_ERROR = "I couldn't define that. Please try again."
_STORAGE_ERROR = "I couldn't save that. Please try again."
_REVIEW_ERROR = "I couldn't check your review. Please try again."
_TEST_ERROR = "I couldn't check your test. Please try again."
_HINT_REQUESTS = frozenset(
    {
        "hint",
        "give me a hint",
        "can i have a hint",
        "show me an example",
        "example sentence",
    }
)


def _is_hint_request(message: str) -> bool:
    normalized = " ".join(message.split()).casefold()
    normalized = normalized.rstrip("?.!").rstrip()
    return normalized in _HINT_REQUESTS




def _is_slash_command(message: str) -> bool:
    if not message.startswith("/"):
        return False
    token = message.split(maxsplit=1)[0][1:]
    if "@" in token:
        token = token.split("@", 1)[0]
    return bool(token) and "/" not in token


class VocabularyGatewayRouter:
    def __init__(
        self,
        capture_service: CaptureService,
        review_service: ReviewService,
        test_service: TestSessionService,
        definition_provider: DefinitionProvider,
        evaluation_provider: EvaluationProvider,
        telegram_chat_id: int,
    ) -> None:
        self._capture_service = capture_service
        self._review_service = review_service
        self._test_service = test_service
        self._definition_provider = definition_provider
        self._evaluation_provider = evaluation_provider
        self._telegram_chat_id = str(telegram_chat_id)
        self._inflight: dict[str, asyncio.Task[str]] = {}
        self._inflight_guard = asyncio.Lock()

    async def route(
        self,
        *,
        platform: str | None,
        sender_id: str | None = None,
        chat_id: str | None,
        chat_type: str | None,
        thread_id: str | None,
        user_message: str,
        **reply_metadata: object,
    ) -> str | None:
        del sender_id, reply_metadata
        if (
            platform != "telegram"
            or chat_id is None
            or str(chat_id) != self._telegram_chat_id
            or chat_type != "dm"
            or thread_id is not None
        ):
            return None
        if _is_slash_command(user_message):
            return None

        pending = self._review_service.pending_review()
        if pending.status is PendingReviewStatus.STORAGE_ERROR:
            return _REVIEW_ERROR
        if pending.status is PendingReviewStatus.PENDING:
            if pending.entry is None:
                return _REVIEW_ERROR
            if _is_hint_request(user_message):
                return format_hint(pending.entry)
            completion = await complete_pending_review(
                self._review_service,
                self._evaluation_provider,
                user_message,
            )
            return format_review_completion(completion)

        test_state = self._test_service.current()
        if test_state.status is TestSnapshotStatus.STORAGE_ERROR:
            return _TEST_ERROR
        if test_state.status is TestSnapshotStatus.ACTIVE:
            if (
                test_state.snapshot is None
                or test_state.snapshot.current_question is None
            ):
                return _TEST_ERROR
            if _is_hint_request(user_message):
                return format_hint(test_state.snapshot.current_question.entry)
            completion = await complete_test_question(
                self._test_service,
                self._evaluation_provider,
                user_message,
            )
            return format_test_completion(completion)

        normalized = normalize_entry_text(user_message)
        if normalized.status is EntryTextStatus.EMPTY:
            return _EMPTY
        if normalized.status is EntryTextStatus.TOO_LONG:
            return _TOO_LONG

        try:
            existing = self._capture_service.get_entry(normalized.normalized_text)
        except sqlite3.Error:
            return _STORAGE_ERROR
        if existing is not None:
            return format_entry(existing, "Already saved.")

        async with self._inflight_guard:
            task = self._inflight.get(normalized.normalized_text)
            if task is None:
                task = asyncio.create_task(
                    self._run_owner(
                        normalized.normalized_text,
                        normalized.display_text,
                    )
                )
                self._inflight[normalized.normalized_text] = task
        return await asyncio.shield(task)

    async def _run_owner(self, normalized_text: str, display_text: str) -> str:
        try:
            return await self._enrich(normalized_text, display_text)
        finally:
            current = asyncio.current_task()
            async with self._inflight_guard:
                if self._inflight.get(normalized_text) is current:
                    self._inflight.pop(normalized_text, None)

    async def _enrich(self, normalized_text: str, display_text: str) -> str:
        try:
            existing = self._capture_service.get_entry(normalized_text)
        except sqlite3.Error:
            return _STORAGE_ERROR
        if existing is not None:
            return format_entry(existing, "Already saved.")

        definition = await self._definition_provider.define(display_text)
        if definition.status is DefinitionStatus.NOT_FOUND:
            return _NOT_FOUND
        if definition.status is not DefinitionStatus.FOUND:
            return _DEFINITION_ERROR

        result = self._capture_service.capture_entry(display_text, definition.cards)
        if result.status is CaptureStatus.STORAGE_ERROR:
            return _STORAGE_ERROR
        if result.status is CaptureStatus.INVALID or result.entry is None:
            return _DEFINITION_ERROR
        footer = (
            "✓ Saved."
            if result.status is CaptureStatus.SAVED
            else "Already saved."
        )
        return format_entry(result.entry, footer)
