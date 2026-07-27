from __future__ import annotations

import asyncio
import json
import sqlite3

from hermes_vocab.capture import CaptureService, normalize_entry_text
from hermes_vocab.formatting import format_entry, format_hint
from hermes_vocab.models import (
    CaptureStatus,
    EntryTextStatus,
    StudyMode,
    StudyPromptSnapshot,
    StudyPromptStatus,
    StudySessionStatus,
    StudyStartResult,
    StudyStartStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService

from .definition import DefinitionProvider, DefinitionStatus
from .evaluation import EvaluationProvider
from .hooks import VocabularyHook
from .tools import ToolHandlers

_EMPTY = "Send a word or phrase."
_TOO_LONG = "Send a word or phrase under 500 characters."
_NOT_FOUND = "I couldn't define that. Please try another word or phrase."
_DEFINITION_ERROR = "I couldn't define that. Please try again."
_STORAGE_ERROR = "I couldn't save that. Please try again."
_REVIEW_ERROR = "I couldn't load that review. Please try again."
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


class CorrelatedText(str):
    correlation_id: str

    def __new__(cls, text: str, correlation_id: str):
        value = super().__new__(cls, text)
        value.correlation_id = correlation_id
        return value


class VocabularyGatewayRouter:
    def __init__(
        self,
        capture_service: CaptureService,
        review_service: ReviewService,
        test_service: TestSessionService,
        definition_provider: DefinitionProvider,
        evaluation_provider: EvaluationProvider,
        telegram_chat_id: int,
        delivery_hook: VocabularyHook | None = None,
    ) -> None:
        self._capture_service = capture_service
        self._review_service = review_service
        self._test_service = test_service
        self._definition_provider = definition_provider
        self._evaluation_provider = evaluation_provider
        self._telegram_chat_id = str(telegram_chat_id)
        self._delivery_hook = delivery_hook
        self._handlers = ToolHandlers(
            capture_service,
            review_service,
            evaluation_provider,
            test_service,
        )
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
        awaiting_rating = self._review_service.awaiting_rating()
        if awaiting_rating is not None:
            return await self._continue_study(user_message)

        answerable = self._review_service.answerable_prompt()
        if answerable is not None:
            if _is_hint_request(user_message):
                context = self._review_service.current_answer_context()
                if context is None:
                    return _REVIEW_ERROR
                return format_hint(context.entry)
            return await self._continue_study(user_message)

        snapshot = self._review_service.snapshot()
        prompt = snapshot.current_prompt if snapshot is not None else None
        if prompt is not None and prompt.status is StudyPromptStatus.PREPARED:
            # The prompt was never confirmed delivered, so this message cannot be
            # an answer. Surface the outstanding question and hand the message
            # back rather than grading, capturing, or silently dropping it.
            label = (
                "Review due."
                if snapshot.mode is StudyMode.REVIEW
                else "Test in progress."
            )
            return self.correlate(
                prompt,
                f"{label} Answer this delivered question first:\n\n"
                f"{prompt.prompt_text}\n\n"
                "Your original message was:\n"
                f"{user_message}\n\n"
                "Complete or exit the study session, then resubmit it.",
            )

        # A review session whose prepared prompt was cancelled by a day
        # rollover is active but has no live prompt; it needs surfacing just
        # like the no-session case below.
        rollover_gap = (
            snapshot is not None
            and prompt is None
            and snapshot.mode is StudyMode.REVIEW
            and snapshot.status is StudySessionStatus.ACTIVE
        )
        if rollover_gap or (
            snapshot is None
            and self._review_service.due_but_not_answerable()
            and not self._review_service.study_was_exited()
        ):
            # Due work exists but no prompt is outstanding (for example the
            # computer was off, or the day rolled over). Ordinary text must
            # surface that work instead of being graded or captured.
            started = (
                StudyStartResult(StudyStartStatus.RESUMED)
                if rollover_gap
                else self._review_service.start()
            )
            if started.status in {
                StudyStartStatus.STARTED,
                StudyStartStatus.RESUMED,
            }:
                prepared = self.prepare_review_prompt()
                if prepared is not None:
                    return self.correlate(
                        prepared,
                        "Review due. Answer this delivered question first:\n\n"
                        f"{prepared.prompt_text}\n\n"
                        "Your original message was:\n"
                        f"{user_message}\n\n"
                        "Complete or exit the review, then resubmit it.",
                    )
            return _REVIEW_ERROR

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

    async def _continue_study(self, user_message: str) -> str:
        payload = json.loads(
            await self._handlers.continue_study({"answer_text": user_message})
        )
        text = str(payload["text"])
        if payload["status"] == "finalized":
            prompt = self.prepare_review_prompt()
            if prompt is not None:
                if prompt.prompt_text not in text:
                    text = f"{text}\n\n{prompt.prompt_text}"
                return self.correlate(prompt, text)
        return text

    def prepare_review_prompt(self) -> StudyPromptSnapshot | None:
        snapshot = self._review_service.snapshot()
        if (
            snapshot is None
            or snapshot.status is not StudySessionStatus.ACTIVE
            or snapshot.mode is not StudyMode.REVIEW
        ):
            return None
        if snapshot.current_prompt is not None:
            return snapshot.current_prompt
        with self._review_service.database.connect() as connection:
            row = connection.execute(
                """
                SELECT q.id AS queue_id, e.display_text
                FROM study_queue q
                JOIN vocabulary_cards c ON c.id = q.card_id
                JOIN vocabulary_entries e ON e.id = c.entry_id
                WHERE q.session_id = ? AND q.status = 'current'
                """,
                (snapshot.session_id,),
            ).fetchone()
        if row is None:
            return None
        current = min(snapshot.progress.completed + 1, snapshot.progress.total)
        due_backlog = self._review_service.due_count()
        text = (
            f"Review {current} of {snapshot.progress.total} · {due_backlog} due\n"
            f"What does '{row['display_text']}' mean?"
        )
        return self._review_service.prepare_current_prompt(
            f"review:{snapshot.session_id}:{row['queue_id']}",
            text,
        )
    def correlate(self, prompt: StudyPromptSnapshot, text: str) -> CorrelatedText:
        if self._delivery_hook is not None:
            self._delivery_hook.prepare_outbound(
                prompt_id=prompt.id,
                identity=prompt.prompt_key,
                text=text,
            )
        return CorrelatedText(text, prompt.prompt_key)

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
