from __future__ import annotations

import asyncio
import json
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
    SHOW_ANSWER_FEEDBACK,
)
from hermes_vocab.hermes_plugin.gateway import VocabularyGatewayRouter
from hermes_vocab.test_session import TestSessionService as SessionService
from hermes_vocab.hermes_plugin.tools import ToolHandlers
from hermes_vocab.models import (
    CaptureStatus,
    EntryCaptureResult,
    Evaluation,
    EvaluationGrade,
    PendingReviewStatus,
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
    test_session = SessionService(database, clock=lambda: NOW)
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


def test_pending_review_is_semantically_evaluated_and_formatted_grade_first(
    tmp_path: Path,
) -> None:
    evaluator = FakeEvaluationProvider(
        EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(EvaluationGrade.PARTIAL, "Right direction, but incomplete."),
        )
    )
    router, capture, review, provider = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    pending = review.daily_review()
    assert pending.event is not None
    answer = "Something brief."

    result = asyncio.run(route(router, answer))

    assert result == (
        "Grade: Partial\n"
        "Feedback: Right direction, but incomplete.\n\n"
        "Definition:\nUsing few words.\n\n"
        "Example:\nHis reply was laconic."
    )
    assert provider.calls == []
    assert len(evaluator.calls) == 1
    assert evaluator.calls[0][1] == answer
    with capture.database.connect() as connection:
        event = connection.execute(
            "SELECT status, answer_text, grade, evaluation_feedback FROM review_events"
        ).fetchone()
    assert tuple(event) == (
        "answered",
        answer,
        "partial",
        "Right direction, but incomplete.",
    )


@pytest.mark.parametrize("hint_request", HINT_REQUESTS)
def test_daily_review_hint_preserves_pending_answer_state(
    tmp_path: Path,
    hint_request: str,
) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (
            SenseCard("adjective", "Using few words.", "His reply was laconic."),
            SenseCard(
                "noun",
                "A concise expression.",
                "The laconic ended the note.",
            ),
        ),
    )
    started = review.daily_review()
    assert started.event is not None
    event_id = started.event.id

    result = asyncio.run(route(router, hint_request))

    assert result == "Hint: His reply was laconic."
    assert evaluator.calls == []
    pending = review.pending_review()
    assert pending.status is PendingReviewStatus.PENDING
    assert pending.event is not None
    assert pending.event.id == event_id
    assert pending.event.answer_text is None
    assert pending.event.grade is None
    assert pending.event.feedback is None
    assert pending.event.answered_at is None
    assert pending.entry is not None
    assert pending.entry.display_text == "laconic"
    assert pending.entry.last_reviewed is None


def test_daily_review_answer_after_hint_grades_same_entry(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    review.daily_review()

    assert asyncio.run(route(router, "give me a hint")) == (
        "Hint: His reply was laconic."
    )
    response = asyncio.run(route(router, "brief or concise"))

    assert response is not None and response.startswith("Grade: Correct")
    assert evaluator.calls[0][0].display_text == "laconic"
    assert evaluator.calls[0][1] == "brief or concise"


def test_evaluator_failure_keeps_pending_and_does_not_reveal(
    tmp_path: Path,
) -> None:
    evaluator = FakeEvaluationProvider(
        EvaluationResult(EvaluationStatus.PROVIDER_ERROR)
    )
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    review.daily_review()

    result = asyncio.run(route(router, "my attempt"))

    assert result == "I couldn't evaluate that answer. Please try again."
    assert "Using few words." not in result
    with capture.database.connect() as connection:
        event = connection.execute(
            "SELECT status, answer_text, grade FROM review_events"
        ).fetchone()
    assert tuple(event) == ("pending", None, None)


def test_exact_show_answer_bypasses_evaluator_and_reveals(
    tmp_path: Path,
) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    review.daily_review()

    result = asyncio.run(route(router, "show answer"))

    assert result == (
        "Grade: Incorrect\n"
        f"Feedback: {SHOW_ANSWER_FEEDBACK}\n\n"
        "Definition:\nUsing few words.\n\n"
        "Example:\nHis reply was laconic."
    )
    assert evaluator.calls == []


def test_plain_answer_is_sent_to_evaluator(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    review.daily_review()

    asyncio.run(route(router, "answer"))

    assert evaluator.calls[0][1] == "answer"


def test_gateway_and_async_tool_share_completion_behavior(tmp_path: Path) -> None:
    result = EvaluationResult(
        EvaluationStatus.VALID,
        Evaluation(EvaluationGrade.PARTIAL, "Right direction, but incomplete."),
    )
    gateway_evaluator = FakeEvaluationProvider(result)
    gateway, gateway_capture, gateway_review, _ = make_router(
        tmp_path / "gateway",
        evaluator=gateway_evaluator,
    )
    tool_evaluator = FakeEvaluationProvider(result)
    _, tool_capture, tool_review, _ = make_router(
        tmp_path / "tool",
        evaluator=tool_evaluator,
    )
    card = SenseCard(
        "adjective",
        "Using few words.",
        "His reply was laconic.",
    )
    gateway_capture.capture_entry("laconic", (card,))
    tool_capture.capture_entry("laconic", (card,))
    gateway_review.daily_review()
    tool_review.daily_review()
    answer = "Something brief."

    gateway_text = asyncio.run(route(gateway, answer))
    tool_payload = json.loads(
        asyncio.run(
            ToolHandlers(
                tool_capture,
                tool_review,
                tool_evaluator,
            ).complete_review({"answer_text": answer})
        )
    )

    assert tool_payload == {
        "status": "completed",
        "text": gateway_text,
    }
    for capture in (gateway_capture, tool_capture):
        with capture.database.connect() as connection:
            stored = connection.execute(
                "SELECT answer_text, grade, evaluation_feedback FROM review_events"
            ).fetchone()
        assert tuple(stored) == (
            answer,
            "partial",
            "Right direction, but incomplete.",
        )


def test_pending_review_consumes_entire_original_message_first(tmp_path: Path) -> None:
    router, capture, review, provider = make_router(tmp_path)
    capture.capture_entry("laconic", (SenseCard("adjective", "Using few words.", "His reply was laconic."),))
    review.daily_review()
    answer = "I think it means brief.\nThat is my whole answer."

    result = asyncio.run(route(router, answer))

    assert result == (
        "Grade: Correct\n"
        "Feedback: Accurate paraphrase.\n\n"
        "Definition:\nUsing few words.\n\n"
        "Example:\nHis reply was laconic."
    )
    assert provider.calls == []
    with capture.database.connect() as connection:
        stored = connection.execute("SELECT answer_text FROM review_events").fetchone()[0]
    assert stored == answer


def test_active_test_is_routed_after_pending_review_check(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider(
        EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(EvaluationGrade.PARTIAL, "Directionally right."),
        )
    )
    router, capture, _, provider = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    started = router._test_service.start()
    assert started.snapshot is not None

    result = asyncio.run(route(router, "my first answer"))

    assert result == (
        "Grade: Partial\n"
        "Feedback: Directionally right.\n\n"
        "Definition:\nDefinition 0.\n\n"
        "Example:\nExample 0.\n\n"
        "Question 2 of 5\n"
        "What does 'word-1' mean?"
    )
    assert provider.calls == []
    assert evaluator.calls[0][0].display_text == "word-0"
    assert evaluator.calls[0][1] == "my first answer"
    current = router._test_service.current().snapshot.current_question
    assert current is not None and current.position == 2


@pytest.mark.parametrize("hint_request", HINT_REQUESTS)
def test_active_test_hint_does_not_evaluate_or_advance(
    tmp_path: Path,
    hint_request: str,
) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (
            SenseCard("adjective", "Using few words.", "His reply was laconic."),
            SenseCard(
                "noun",
                "A concise expression.",
                "The laconic ended the note.",
            ),
        ),
    )
    add_test_entries(capture, count=4)
    before = router._test_service.start().snapshot
    assert before is not None and before.current_question is not None
    question_id = before.current_question.id
    totals = (
        before.summary.correct,
        before.summary.partial,
        before.summary.incorrect,
    )

    result = asyncio.run(route(router, hint_request))

    assert result == "Hint: His reply was laconic."
    assert evaluator.calls == []
    after = router._test_service.current().snapshot
    assert after is not None and after.current_question is not None
    assert after.current_question.id == question_id
    assert after.current_question.position == 1
    assert after.current_question.entry.display_text == "laconic"
    assert after.current_question.answer_text is None
    assert after.current_question.grade is None
    assert after.current_question.feedback is None
    assert after.current_question.answered_at is None
    assert (
        after.summary.correct,
        after.summary.partial,
        after.summary.incorrect,
    ) == totals


def test_active_test_answer_after_hint_grades_same_entry(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    add_test_entries(capture, count=4)
    router._test_service.start()

    assert asyncio.run(route(router, "give me a hint")) == (
        "Hint: His reply was laconic."
    )
    response = asyncio.run(route(router, "brief or concise"))

    assert response is not None and response.startswith("Grade: Correct")
    assert evaluator.calls[0][0].display_text == "laconic"
    assert evaluator.calls[0][1] == "brief or concise"
    current = router._test_service.current().snapshot.current_question
    assert current is not None and current.position == 2


def test_nonmatching_hint_text_is_still_evaluated_during_test(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    router._test_service.start()

    asyncio.run(route(router, "give me another hint"))

    assert evaluator.calls[0][1] == "give me another hint"
    current = router._test_service.current().snapshot.current_question
    assert current is not None and current.position == 2


def test_hint_phrase_outside_study_flow_uses_capture_route(tmp_path: Path) -> None:
    router, capture, _, provider = make_router(tmp_path)

    result = asyncio.run(route(router, "hint"))

    assert provider.calls == ["hint"]
    assert result is not None
    assert capture.get_entry("hint") is not None


def test_concurrent_test_replies_only_advance_the_prepared_question_once(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        evaluator = BlockingEvaluationProvider()
        router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
        add_test_entries(capture)
        router._test_service.start()

        replies = [
            asyncio.create_task(route(router, answer))
            for answer in ("first answer", "second answer")
        ]
        await asyncio.wait_for(evaluator.both_started.wait(), timeout=1)
        evaluator.release.set()
        results = await asyncio.gather(*replies)

        stale_reply = (
            "That answer was already recorded.\n\n"
            "Question 2 of 5\n"
            "What does 'word-1' mean?"
        )
        assert results.count(stale_reply) == 1
        advanced_replies = [result for result in results if result != stale_reply]
        assert len(advanced_replies) == 1
        assert advanced_replies[0] == (
            "Grade: Correct\n"
            "Feedback: Accurate paraphrase.\n\n"
            "Definition:\nDefinition 0.\n\n"
            "Example:\nExample 0.\n\n"
            "Question 2 of 5\n"
            "What does 'word-1' mean?"
        )

        snapshot = router._test_service.current().snapshot
        assert snapshot is not None
        assert sum(question.answer_text is not None for question in snapshot.questions) == 1
        assert snapshot.current_question is not None
        assert snapshot.current_question.position == 2
        assert snapshot.current_question.answer_text is None
        assert len(evaluator.calls) == 2
        assert all(call[0].display_text == "word-0" for call in evaluator.calls)

    asyncio.run(exercise())


def test_pending_review_takes_precedence_over_active_test_if_state_is_corrupt(
    tmp_path: Path,
) -> None:
    router, capture, review, _ = make_router(tmp_path)
    add_test_entries(capture)
    started = router._test_service.start()
    assert started.snapshot is not None
    with capture.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO review_events (
                entry_id, review_date, status, prompted_at
            )
            VALUES (?, '2026-07-17', 'pending', '2026-07-17T12:00:00Z')
            """,
            (started.snapshot.questions[-1].entry.id,),
        )
        connection.commit()

    result = asyncio.run(route(router, "daily answer"))

    assert result is not None and "Grade: Correct" in result
    evaluator = router._evaluation_provider
    assert evaluator.calls[0][0].display_text == "word-4"
    current = router._test_service.current().snapshot.current_question
    assert current is not None and current.position == 1


def test_test_show_answer_bypasses_evaluator_and_advances(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    router._test_service.start()

    result = asyncio.run(route(router, "show answer"))

    assert result == (
        "Grade: Incorrect\n"
        f"Feedback: {SHOW_ANSWER_FEEDBACK}\n\n"
        "Definition:\nDefinition 0.\n\n"
        "Example:\nExample 0.\n\n"
        "Question 2 of 5\n"
        "What does 'word-1' mean?"
    )
    assert evaluator.calls == []
    snapshot = router._test_service.current().snapshot
    assert snapshot.summary.incorrect == 1
    assert snapshot.current_question.position == 2


def test_test_evaluator_failure_does_not_advance_or_reveal(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider(
        EvaluationResult(EvaluationStatus.PROVIDER_ERROR)
    )
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    started = router._test_service.start().snapshot
    question_id = started.current_question.id

    result = asyncio.run(route(router, "attempt"))

    assert result == "I couldn't evaluate that answer. Please try again."
    assert "Definition 0." not in result
    current = router._test_service.current().snapshot.current_question
    assert current.id == question_id
    assert current.answer_text is None


def test_five_test_answers_finish_with_exact_category_totals(
    tmp_path: Path,
) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, provider = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    router._test_service.start()
    grades = [
        EvaluationGrade.CORRECT,
        EvaluationGrade.PARTIAL,
        EvaluationGrade.INCORRECT,
        EvaluationGrade.CORRECT,
        EvaluationGrade.PARTIAL,
    ]

    responses = []
    for index, grade in enumerate(grades, start=1):
        evaluator.result = EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(grade, f"Feedback {index}."),
        )
        responses.append(asyncio.run(route(router, f"answer {index}")))

    assert all(
        f"Question {index + 1} of 5" in responses[index - 1]
        for index in range(1, 5)
    )
    assert responses[-1].endswith(
        "Test complete.\n"
        "Results: 2 correct, 2 partial, 1 incorrect."
    )
    assert router._test_service.current().snapshot is None
    assert provider.calls == []
    assert len(evaluator.calls) == 5


def test_completed_test_falls_through_to_normal_lookup(tmp_path: Path) -> None:
    router, capture, _, provider = make_router(tmp_path)
    add_test_entries(capture)
    router._test_service.start()
    for index in range(5):
        asyncio.run(route(router, f"answer {index}"))

    result = asyncio.run(route(router, "word-0"))

    assert result is not None and result.endswith("Already saved.")
    assert provider.calls == []


def test_test_answer_continuation_is_root_dm_only_and_slash_bypassed(
    tmp_path: Path,
) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    router._test_service.start()

    assert asyncio.run(route(router, "/test")) is None
    assert asyncio.run(route(router, "answer", chat_id="elsewhere")) is None
    assert asyncio.run(route(router, "answer", chat_type="group")) is None
    assert asyncio.run(route(router, "answer", thread_id="topic")) is None
    assert asyncio.run(route(router, "answer", platform="discord")) is None
    assert evaluator.calls == []
    assert router._test_service.current().snapshot.current_question.position == 1


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
