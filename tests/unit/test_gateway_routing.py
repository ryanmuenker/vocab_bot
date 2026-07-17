from __future__ import annotations

import asyncio
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
from hermes_vocab.hermes_plugin.gateway import VocabularyGatewayRouter
from hermes_vocab.models import CaptureStatus, EntryCaptureResult, SenseCard
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


def make_router(
    tmp_path: Path,
    provider: FakeProvider | None = None,
) -> tuple[VocabularyGatewayRouter, CaptureService, ReviewService, FakeProvider]:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    capture = CaptureService(database, clock=lambda: NOW)
    review = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW)
    definition = provider or FakeProvider()
    return (
        VocabularyGatewayRouter(capture, review, definition, CHAT_ID),
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


def test_pending_review_consumes_entire_original_message_first(tmp_path: Path) -> None:
    router, capture, review, provider = make_router(tmp_path)
    capture.capture_entry("laconic", (SenseCard("adjective", "Using few words.", "His reply was laconic."),))
    review.daily_review()
    answer = "I think it means brief.\nThat is my whole answer."

    result = asyncio.run(route(router, answer))

    assert result == "Definition:\nUsing few words.\n\nExample:\nHis reply was laconic."
    assert provider.calls == []
    with capture.database.connect() as connection:
        stored = connection.execute("SELECT answer_text FROM review_events").fetchone()[0]
    assert stored == answer


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


def test_review_state_failure_is_handled_without_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    router, _, review, provider = make_router(tmp_path)

    def fail_connect():
        raise sqlite3.OperationalError("private state")

    monkeypatch.setattr(review.database, "connect", fail_connect)

    assert asyncio.run(route(router, "perfidy")) == (
        "I couldn't check your review. Please try again."
    )
    assert provider.calls == []


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
