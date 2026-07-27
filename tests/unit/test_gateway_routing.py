from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from types import SimpleNamespace
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes_vocab.capture import CaptureService
from hermes_vocab.database import Database
from hermes_vocab.hermes_plugin.definition import (
    DefinitionResult,
    DefinitionStatus,
)
from hermes_vocab.hermes_plugin.evaluation import (
    EvaluationResult,
    EvaluationStatus,
)
from hermes_vocab.hermes_plugin.gateway import VocabularyGatewayRouter
from hermes_vocab.hermes_plugin.hooks import VocabularyHook
from hermes_vocab.test_session import TestSessionService as SessionService
from hermes_vocab.models import (
    CaptureStatus,
    EntryCaptureResult,
    Evaluation,
    EvaluationGrade,
    SenseCard,
)
from hermes_vocab.review import ReviewService

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)
CHAT_ID = 7747352551
CARDS = (
    SenseCard(
        "adjective",
        "Provided as a matter of form.",
        "The board issued a pro forma approval.",
    ),
    SenseCard(
        "noun",
        "A projected financial statement.",
        "The analyst prepared a pro forma.",
    ),
)

HINT_REQUESTS = [
    "hint",
    "HINT",
    "  give   me a hint  ",
    "Can I have a hint?",
    "show me an example.",
    "example sentence!",
]


class FakeProvider:
    def __init__(self, result: DefinitionResult | None = None) -> None:
        self.result = result or DefinitionResult(DefinitionStatus.FOUND, CARDS)
        self.calls: list[str] = []
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def define(self, display_text: str) -> DefinitionResult:
        self.calls.append(display_text)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return self.result


class FakeEvaluationProvider:
    def __init__(self, result: EvaluationResult | None = None) -> None:
        self.result = result or EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(EvaluationGrade.CORRECT, "Accurate paraphrase."),
        )
        self.calls: list[tuple[object, str]] = []

    async def evaluate(self, entry, answer_text: str) -> EvaluationResult:
        self.calls.append((entry, answer_text))
        return self.result

class BlockingEvaluationProvider(FakeEvaluationProvider):
    def __init__(self) -> None:
        super().__init__()
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def evaluate(self, entry, answer_text: str) -> EvaluationResult:
        self.calls.append((entry, answer_text))
        if len(self.calls) == 2:
            self.both_started.set()
        await self.release.wait()
        return self.result


def make_router(
    tmp_path: Path,
    provider: FakeProvider | None = None,
    evaluator: FakeEvaluationProvider | None = None,
) -> tuple[VocabularyGatewayRouter, CaptureService, ReviewService, FakeProvider]:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    capture = CaptureService(database, clock=lambda: NOW)
    review = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW)
    definition = provider or FakeProvider()
    evaluation = evaluator or FakeEvaluationProvider()
    test_session = SessionService(database, ZoneInfo("UTC"), clock=lambda: NOW)
    return (
        VocabularyGatewayRouter(
            capture,
            review,
            test_session,
            definition,
            evaluation,
            CHAT_ID,
        ),
        capture,
        review,
        definition,
    )


async def route(router: VocabularyGatewayRouter, message: str, **overrides) -> str | None:
    kwargs = {
        "platform": "telegram",
        "sender_id": "42",
        "chat_id": str(CHAT_ID),
        "chat_type": "dm",
        "thread_id": None,
        "user_message": message,
    }
    kwargs.update(overrides)
    return await router.route(**kwargs)



def add_test_entries(capture: CaptureService, count: int = 5) -> None:
    for index in range(count):
        capture.capture_entry(
            f"word-{index}",
            (
                SenseCard(
                    "noun",
                    f"Definition {index}.",
                    f"Example {index}.",
                ),
            ),
        )


OVERDUE_PROMPT_TEXT = "Review 1 of 1 · 1 due\nWhat does 'laconic' mean?"


def make_overdue_forward_only(capture: CaptureService, entry_id: int) -> None:
    """Leave exactly one overdue forward card for the entry capture just saved."""
    with capture.database.connect() as connection:
        connection.execute(
            "DELETE FROM vocabulary_cards WHERE entry_id = ? AND direction = 'reverse'",
            (entry_id,),
        )
        connection.execute(
            """
            UPDATE vocabulary_cards
            SET state = 'review', stability = 2.0, difficulty = 5.0,
                due_at = ?, effective_due_at = ?, last_review_at = ?,
                repetitions = 1, lapses = 0, created_at = ?
            WHERE entry_id = ? AND direction = 'forward'
            """,
            (
                "2026-07-16T12:00:00Z",
                "2026-07-16T12:00:00Z",
                "2026-07-10T12:00:00Z",
                "2026-07-01T12:00:00Z",
                entry_id,
            ),
        )
        connection.commit()


def seed_overdue_review_prompt(
    capture: CaptureService,
    review: ReviewService,
    *,
    prompt_key: str,
):
    """Persist one overdue forward card and prepare its review prompt."""
    captured = capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    assert captured.entry is not None
    make_overdue_forward_only(capture, captured.entry.id)
    started = review.start()
    assert started.snapshot is not None
    prompt = review.prepare_current_prompt(prompt_key, OVERDUE_PROMPT_TEXT)
    assert prompt is not None
    return prompt


def seed_overdue_card_without_session(capture: CaptureService) -> None:
    """Persist one overdue forward card and start no study session at all."""
    captured = capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    assert captured.entry is not None
    make_overdue_forward_only(capture, captured.entry.id)


def test_undelivered_due_work_interrupts_capture_when_no_session_exists(
    tmp_path: Path,
) -> None:
    """Covers AE2/R4 when cron never ran: due work must never be silently captured."""
    router, capture, review, definition = make_router(tmp_path)
    seed_overdue_card_without_session(capture)
    assert review.snapshot() is None
    assert review.due_but_not_answerable() is True

    response = asyncio.run(route(router, "Xanthocroid"))

    assert response is not None
    assert "What does 'laconic' mean?" in response
    assert "Xanthocroid" in response
    assert "resubmit" in response
    assert definition.calls == []
    assert router._evaluation_provider.calls == []
    assert capture.get_entry("xanthocroid") is None
    assert review.answerable_prompt() is None
    with capture.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM study_prompts"
        ).fetchone()[0] == 1


def test_review_error_paths_return_copy_instead_of_raising(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The review-unavailable branches must be reachable copy, not a NameError."""
    router, capture, review, definition = make_router(tmp_path)
    prompt = seed_overdue_review_prompt(capture, review, prompt_key="review:hint-error")
    assert review.record_delivery(
        prompt.id,
        delivery_id="delivered",
        content_fingerprint="fingerprint",
    ) is not None
    monkeypatch.setattr(review, "current_answer_context", lambda: None)

    assert asyncio.run(route(router, "hint")) == (
        "I couldn't load that review. Please try again."
    )
    assert definition.calls == []
    assert router._evaluation_provider.calls == []


def cron_receipt(
    state: str,
    run_id: str,
    fingerprint: str,
    *,
    message_ids: tuple[str, ...] = (),
    error: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        destination=f"telegram:{CHAT_ID}",
        message_ids=message_ids,
        correlation_id=None,
        cron_run_id=run_id,
        content_fingerprint=fingerprint,
        error=error,
    )



def test_prepared_prompt_for_new_cards_still_echoes_the_original_message(
    tmp_path: Path,
) -> None:
    """R4: an intercepted message must never vanish without a resubmit request."""
    router, capture, review, definition = make_router(tmp_path)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    review.start()
    prepared = router.prepare_review_prompt()
    assert prepared is not None
    assert review.answerable_prompt() is None

    response = asyncio.run(route(router, "Xanthocroid"))

    assert response is not None
    assert prepared.prompt_text in response
    assert "Xanthocroid" in response
    assert "resubmit" in response
    assert definition.calls == []
    assert router._evaluation_provider.calls == []
    assert capture.get_entry("xanthocroid") is None


def interactive_receipt(
    state: str,
    prompt_key: str,
    fingerprint: str,
    *,
    message_ids: tuple[str, ...] = (),
    error: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        destination=f"telegram:{CHAT_ID}",
        message_ids=message_ids,
        correlation_id=prompt_key,
        cron_run_id=None,
        content_fingerprint=fingerprint,
        error=error,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"platform": "discord"},
        {"chat_id": "1"},
        {"chat_type": "private"},
        {"chat_type": None},
        {"thread_id": "topic"},
        {"user_message": "/help"},
    ],
)
def test_router_declines_every_non_dedicated_lane(tmp_path: Path, overrides: dict) -> None:
    router, _, _, provider = make_router(tmp_path)

    assert asyncio.run(route(router, "perfidy", **overrides)) is None
    assert provider.calls == []


def test_slash_prefixed_path_is_routed_as_non_command_entry(tmp_path: Path) -> None:
    router, _, _, provider = make_router(tmp_path)

    result = asyncio.run(route(router, "/tmp/vocabulary"))

    assert result is not None and result.endswith("✓ Saved.")
    assert provider.calls == ["/tmp/vocabulary"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("  ", "Send a word or phrase."),
        ("x" * 501, "Send a word or phrase under 500 characters."),
    ],
)
def test_router_rejects_invalid_entry_text_without_provider_or_writes(
    tmp_path: Path,
    message: str,
    expected: str,
) -> None:
    router, capture, _, provider = make_router(tmp_path)

    assert asyncio.run(route(router, message)) == expected
    assert provider.calls == []
    with capture.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_entries").fetchone()[0] == 0


























def test_hint_phrase_outside_study_flow_uses_capture_route(tmp_path: Path) -> None:
    router, capture, _, provider = make_router(tmp_path)

    result = asyncio.run(route(router, "hint"))

    assert provider.calls == ["hint"]
    assert result is not None
    assert capture.get_entry("hint") is not None
















def test_stored_entry_returns_every_sense_without_provider_or_write(tmp_path: Path) -> None:
    router, capture, _, provider = make_router(tmp_path)
    saved = capture.capture_entry("Pro   Forma", CARDS)
    with capture.database.connect() as connection:
        before = (
            connection.execute("SELECT COUNT(*) FROM vocabulary_entries").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0],
        )

    result = asyncio.run(route(router, "PRO FORMA"))

    assert result is not None
    assert "1. adjective" in result
    assert "2. noun" in result
    assert result.endswith("Already saved.")
    assert provider.calls == []
    assert saved.entry.display_text == "Pro   Forma"
    with capture.database.connect() as connection:
        after = (
            connection.execute("SELECT COUNT(*) FROM vocabulary_entries").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0],
        )
    assert after == before


def test_unseen_entry_calls_provider_once_and_formats_committed_aggregate(
    tmp_path: Path,
) -> None:
    router, capture, _, provider = make_router(tmp_path)

    result = asyncio.run(route(router, "Pro   Forma"))

    assert provider.calls == ["Pro   Forma"]
    assert result is not None
    assert "1. adjective" in result
    assert "2. noun" in result
    assert result.endswith("✓ Saved.")
    entry = capture.get_entry("pro forma")
    assert entry is not None
    assert entry.display_text == "Pro   Forma"
    assert len(entry.senses) == 2


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            DefinitionStatus.NOT_FOUND,
            "I couldn't define that. Please try another word or phrase.",
        ),
        (DefinitionStatus.INVALID_RESPONSE, "I couldn't define that. Please try again."),
        (DefinitionStatus.PROVIDER_ERROR, "I couldn't define that. Please try again."),
    ],
)
def test_provider_failures_return_exact_copy_without_writes(
    tmp_path: Path,
    status: DefinitionStatus,
    expected: str,
) -> None:
    provider = FakeProvider(DefinitionResult(status))
    router, capture, _, _ = make_router(tmp_path, provider)

    assert asyncio.run(route(router, "perfidy")) == expected
    with capture.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_entries").fetchone()[0] == 0


def test_lookup_storage_failure_is_handled_without_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    router, capture, _, provider = make_router(tmp_path)

    def fail_lookup(text: str):
        raise sqlite3.OperationalError(f"private {text}")

    monkeypatch.setattr(capture, "get_entry", fail_lookup)

    assert asyncio.run(route(router, "perfidy")) == "I couldn't save that. Please try again."
    assert provider.calls == []

def test_owner_recheck_storage_failure_does_not_call_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    router, capture, _, provider = make_router(tmp_path)
    calls = 0

    def fail_second_lookup(text: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError(f"private {text}")
        return None

    monkeypatch.setattr(capture, "get_entry", fail_second_lookup)

    assert asyncio.run(route(router, "perfidy")) == "I couldn't save that. Please try again."
    assert provider.calls == []



def test_batch_storage_failure_returns_exact_copy(tmp_path: Path, monkeypatch) -> None:
    router, capture, _, provider = make_router(tmp_path)
    monkeypatch.setattr(
        capture,
        "capture_entry",
        lambda display_text, cards: EntryCaptureResult(CaptureStatus.STORAGE_ERROR),
    )

    assert asyncio.run(route(router, "perfidy")) == "I couldn't save that. Please try again."
    assert provider.calls == ["perfidy"]




def test_simultaneous_misses_share_one_provider_and_save(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        provider.started = asyncio.Event()
        provider.release = asyncio.Event()
        router, capture, _, _ = make_router(tmp_path, provider)

        first = asyncio.create_task(route(router, "Pro Forma"))
        await provider.started.wait()
        second = asyncio.create_task(route(router, "PRO   FORMA"))
        await asyncio.sleep(0)
        provider.release.set()
        results = await asyncio.gather(first, second)

        assert provider.calls == ["Pro Forma"]
        assert results[0] == results[1]
        assert router._inflight == {}
        entry = capture.get_entry("pro forma")
        assert entry is not None
        assert len(entry.senses) == 2

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_enrichment(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        provider.started = asyncio.Event()
        provider.release = asyncio.Event()
        router, capture, _, _ = make_router(tmp_path, provider)

        waiter = asyncio.create_task(route(router, "Pro Forma"))
        await provider.started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        provider.release.set()
        while router._inflight:
            await asyncio.sleep(0)

        assert capture.get_entry("pro forma") is not None
        assert provider.calls == ["Pro Forma"]
        cached = await route(router, "PRO FORMA")
        assert cached is not None and cached.endswith("Already saved.")
        assert provider.calls == ["Pro Forma"]

    asyncio.run(scenario())


def test_failed_prepared_review_intercepts_xanthocroid_without_evaluation_or_capture(
    tmp_path: Path,
) -> None:
    router, capture, review, definition = make_router(tmp_path)
    prompt = seed_overdue_review_prompt(
        capture,
        review,
        prompt_key="review:failed-delivery",
    )
    hook = VocabularyHook(capture, review, CHAT_ID)
    hook.post_outbound_delivery(
        receipt=SimpleNamespace(
            state="failure",
            destination=f"telegram:{CHAT_ID}",
            message_ids=(),
            correlation_id=prompt.prompt_key,
            cron_run_id=None,
            content_fingerprint=sha256(prompt.prompt_text.encode()).hexdigest(),
            error="transport unavailable",
        )
    )

    response = asyncio.run(route(router, "Xanthocroid"))

    assert response == (
        "Review due. Answer this delivered question first:\n\n"
        "Review 1 of 1 · 1 due\n"
        "What does 'laconic' mean?\n\n"
        "Your original message was:\n"
        "Xanthocroid\n\n"
        "Complete or exit the study session, then resubmit it."
    )
    assert definition.calls == []
    assert router._evaluation_provider.calls == []
    assert capture.get_entry("xanthocroid") is None
    with capture.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 0

    hook.post_outbound_delivery(
        receipt=SimpleNamespace(
            state="success",
            destination=f"telegram:{CHAT_ID}",
            message_ids=("wrong",),
            correlation_id=prompt.prompt_key,
            cron_run_id=None,
            content_fingerprint="0" * 64,
            error=None,
        )
    )
    assert review.answerable_prompt() is None
    hook.post_outbound_delivery(
        receipt=SimpleNamespace(
            state="success",
            destination=f"telegram:{CHAT_ID}",
            message_ids=("telegram-message-1",),
            correlation_id=prompt.prompt_key,
            cron_run_id=None,
            content_fingerprint=sha256(prompt.prompt_text.encode()).hexdigest(),
            error=None,
        )
    )
    answer = asyncio.run(route(router, "Using few words."))
    assert answer is not None and answer.startswith("Grade: Correct")
    assert router._evaluation_provider.calls[0][1] == "Using few words."


def test_unknown_cron_receipt_stays_retryable_and_a_retry_run_promotes_once(
    tmp_path: Path,
) -> None:
    _, capture, review, _ = make_router(tmp_path)
    prompt = seed_overdue_review_prompt(
        capture,
        review,
        prompt_key="review:unknown-receipt",
    )
    hook = VocabularyHook(capture, review, CHAT_ID)
    fingerprint = sha256(prompt.prompt_text.encode()).hexdigest()

    assert hook.prepare_outbound(
        prompt_id=prompt.id,
        identity="cron-run-1",
        text=prompt.prompt_text,
    )
    hook.post_outbound_delivery(
        receipt=cron_receipt(
            "unknown",
            "cron-run-1",
            fingerprint,
            error="telegram timed out",
        )
    )

    assert review.answerable_prompt() is None
    assert review.due_but_not_answerable() is True

    hook.post_outbound_delivery(
        receipt=cron_receipt(
            "success",
            "cron-run-1",
            fingerprint,
            message_ids=("late-message",),
        )
    )

    assert review.answerable_prompt() is None

    assert hook.prepare_outbound(
        prompt_id=prompt.id,
        identity="cron-run-2",
        text=prompt.prompt_text,
    )
    for _ in range(2):
        hook.post_outbound_delivery(
            receipt=cron_receipt(
                "success",
                "cron-run-2",
                fingerprint,
                message_ids=("telegram-message-9",),
            )
        )

    answerable = review.answerable_prompt()
    assert answerable is not None and answerable.id == prompt.id
    with capture.database.connect() as connection:
        attempts = connection.execute(
            """
            SELECT status, outbound_delivery_id, receipt_at IS NOT NULL
            FROM prompt_delivery_attempts
            WHERE prompt_id = ? ORDER BY attempt_number
            """,
            (prompt.id,),
        ).fetchall()
    assert [tuple(row) for row in attempts] == [
        ("unknown", "cron-run-1", 0),
        ("unknown", "cron-run-1", 1),
        ("unknown", "cron-run-2", 0),
        ("delivered", "telegram-message-9", 1),
    ]


def test_good_is_a_rating_only_while_awaiting_rating(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    prompt = seed_overdue_review_prompt(
        capture,
        review,
        prompt_key="review:rating-token",
    )
    assert review.record_delivery(
        prompt.id,
        delivery_id="telegram-message-1",
        content_fingerprint=sha256(prompt.prompt_text.encode()).hexdigest(),
    ) is not None

    answered = asyncio.run(route(router, "good"))

    assert evaluator.calls == [(evaluator.calls[0][0], "good")]
    assert evaluator.calls[0][0].display_text == "laconic"
    assert answered is not None and answered.startswith("Grade: Correct")
    awaiting = review.awaiting_rating()
    assert awaiting is not None and awaiting.id == prompt.id
    with capture.database.connect() as connection:
        assert connection.execute(
            "SELECT submitted_answer FROM answer_drafts WHERE prompt_id = ?",
            (prompt.id,),
        ).fetchone()[0] == "good"

    rated = asyncio.run(route(router, "good"))

    assert rated is not None and not rated.startswith("Send one of the listed")
    assert len(evaluator.calls) == 1
    assert review.awaiting_rating() is None
    with capture.database.connect() as connection:
        assert connection.execute(
            "SELECT rating FROM review_attempts"
        ).fetchone()[0] == "good"


def test_concurrent_delivery_receipts_promote_one_prompt_once(tmp_path: Path) -> None:
    _, capture, review, _ = make_router(tmp_path)
    prompt = seed_overdue_review_prompt(
        capture,
        review,
        prompt_key="review:concurrent-receipt",
    )
    hook = VocabularyHook(capture, review, CHAT_ID)
    fingerprint = sha256(prompt.prompt_text.encode()).hexdigest()
    assert hook.prepare_outbound(
        prompt_id=prompt.id,
        identity="cron-run-1",
        text=prompt.prompt_text,
    )
    start = threading.Barrier(2)

    def deliver() -> None:
        start.wait(timeout=10)
        hook.post_outbound_delivery(
            receipt=cron_receipt(
                "success",
                "cron-run-1",
                fingerprint,
                message_ids=("telegram-message-1",),
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(deliver), pool.submit(deliver)]:
            future.result(timeout=30)

    answerable = review.answerable_prompt()
    assert answerable is not None and answerable.id == prompt.id
    with capture.database.connect() as connection:
        attempts = connection.execute(
            """
            SELECT status, COUNT(*) FROM prompt_delivery_attempts
            WHERE prompt_id = ? GROUP BY status ORDER BY status
            """,
            (prompt.id,),
        ).fetchall()
    assert [tuple(row) for row in attempts] == [("delivered", 1), ("unknown", 1)]


def test_concurrent_inbound_answers_persist_one_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        evaluator = BlockingEvaluationProvider()
        router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
        prompt = seed_overdue_review_prompt(
            capture,
            review,
            prompt_key="review:concurrent-answer",
        )
        assert review.record_delivery(
            prompt.id,
            delivery_id="telegram-message-1",
            content_fingerprint=sha256(prompt.prompt_text.encode()).hexdigest(),
        ) is not None

        first = asyncio.create_task(route(router, "Using few words."))
        second = asyncio.create_task(route(router, "Terse and sparing."))
        await evaluator.both_started.wait()
        evaluator.release.set()
        results = await asyncio.gather(first, second)

        assert [call[1] for call in evaluator.calls] == [
            "Using few words.",
            "Terse and sparing.",
        ]
        assert all(
            result is not None and result.startswith("Grade: Correct")
            for result in results
        )
        awaiting = review.awaiting_rating()
        assert awaiting is not None and awaiting.id == prompt.id
        with capture.database.connect() as connection:
            drafts = connection.execute(
                "SELECT submitted_answer FROM answer_drafts"
            ).fetchall()
        assert [row[0] for row in drafts] == ["Using few words."]

    asyncio.run(scenario())


def test_interactive_prompt_identity_promotes_after_an_unknown_receipt(
    tmp_path: Path,
) -> None:
    _, capture, review, _ = make_router(tmp_path)
    prompt = seed_overdue_review_prompt(
        capture,
        review,
        prompt_key="review:interactive-retry",
    )
    hook = VocabularyHook(capture, review, CHAT_ID)
    fingerprint = sha256(prompt.prompt_text.encode()).hexdigest()
    assert hook.prepare_outbound(
        prompt_id=prompt.id,
        identity=prompt.prompt_key,
        text=prompt.prompt_text,
    )
    hook.post_outbound_delivery(
        receipt=interactive_receipt(
            "unknown",
            prompt.prompt_key,
            fingerprint,
            error="telegram timed out",
        )
    )

    assert review.answerable_prompt() is None

    assert hook.prepare_outbound(
        prompt_id=prompt.id,
        identity=prompt.prompt_key,
        text=prompt.prompt_text,
    )
    hook.post_outbound_delivery(
        receipt=interactive_receipt(
            "success",
            prompt.prompt_key,
            fingerprint,
            message_ids=("telegram-message-2",),
        )
    )

    answerable = review.answerable_prompt()
    assert answerable is not None and answerable.id == prompt.id


def test_rollover_cancelled_prompt_still_interrupts_capture(
    tmp_path: Path,
) -> None:
    """A day rollover cancels an undelivered prompt; ordinary text must still
    surface the review instead of being silently captured."""
    from datetime import timedelta

    moment = {"now": NOW}
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    capture = CaptureService(database, clock=lambda: moment["now"])
    review = ReviewService(database, ZoneInfo("UTC"), clock=lambda: moment["now"])
    definition = FakeProvider()
    router = VocabularyGatewayRouter(
        capture,
        review,
        SessionService(database, ZoneInfo("UTC"), clock=lambda: moment["now"]),
        definition,
        FakeEvaluationProvider(),
        CHAT_ID,
    )
    capture.capture_entry("laconic", CARDS)
    review.start()
    prepared = router.prepare_review_prompt()
    assert prepared is not None
    assert review.answerable_prompt() is None

    # The next local day begins before the prompt was ever delivered.
    moment["now"] = NOW + timedelta(days=1)

    response = asyncio.run(route(router, "Xanthocroid"))

    assert response is not None
    assert "Answer this delivered question first" in response
    assert "Xanthocroid" in response
    assert definition.calls == []
    assert capture.get_entry("xanthocroid") is None
    # A fresh prompt was prepared for the reshuffled queue in the same turn.
    snapshot = review.snapshot()
    assert snapshot is not None
    assert snapshot.current_prompt is not None
    assert snapshot.current_prompt.id != prepared.id
