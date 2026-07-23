from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hermes_vocab.capture import CaptureService
from hermes_vocab.database import Database

from hermes_vocab.hermes_plugin.evaluation import (
    MAX_EVALUATION_FEEDBACK_LENGTH,
    SHOW_ANSWER_FEEDBACK,
    EvaluationProvider,
    EvaluationResult,
    EvaluationStatus,
    complete_pending_review,
    parse_evaluation_response,
)
from hermes_vocab.models import (
    Evaluation,
    EvaluationGrade,
    ReviewCompletionStatus,
    SenseCard,
    VocabularyEntry,
    VocabularySense,
)
from hermes_vocab.review import ReviewService

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


def entry_with_senses() -> VocabularyEntry:
    return VocabularyEntry(
        id=1,
        display_text="bank",
        normalized_text="bank",
        date_added=NOW,
        last_reviewed=None,
        review_status="new",
        senses=(
            VocabularySense(
                id=11,
                entry_id=1,
                part_of_speech="noun",
                definition="A financial institution.",
                example_sentence="She deposited the cheque at the bank.",
                source_context=None,
                date_added=NOW,
            ),
            VocabularySense(
                id=12,
                entry_id=1,
                part_of_speech="noun",
                definition="Land alongside a river.",
                example_sentence="They sat on the grassy bank.",
                source_context="They reached the bank at dusk.",
                date_added=NOW,
            ),
        ),
    )


@pytest.mark.parametrize(
    "grade",
    [
        EvaluationGrade.CORRECT,
        EvaluationGrade.PARTIAL,
        EvaluationGrade.INCORRECT,
    ],
)
def test_parse_evaluation_response_accepts_only_learner_grades(
    grade: EvaluationGrade,
) -> None:
    result = parse_evaluation_response(
        json.dumps({"grade": grade.value, "feedback": "Specific feedback."})
    )

    assert result.status is EvaluationStatus.VALID
    assert result.evaluation is not None
    assert result.evaluation.grade is grade
    assert result.evaluation.feedback == "Specific feedback."


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        "[]",
        '{"grade":"correct"}',
        '{"feedback":"Useful."}',
        '{"grade":"correct","feedback":"Useful.","extra":true}',
        '{"grade":"mostly_correct","feedback":"Useful."}',
        '{"grade":"CORRECT","feedback":"Useful."}',
        '{"grade":"correct","feedback":""}',
        '{"grade":"correct","feedback":"   "}',
        '{"grade":"correct","feedback":12}',
        json.dumps(
            {
                "grade": "correct",
                "feedback": "x" * (MAX_EVALUATION_FEEDBACK_LENGTH + 1),
            }
        ),
    ],
)
def test_parse_evaluation_response_rejects_non_exact_or_unbounded_payloads(
    response: str,
) -> None:
    result = parse_evaluation_response(response)

    assert result.status is EvaluationStatus.INVALID_RESPONSE
    assert result.evaluation is None


def test_evaluation_provider_makes_one_bounded_tool_free_call_with_all_senses() -> None:
    calls: list[dict] = []

    async def call_llm(**kwargs) -> str:
        calls.append(kwargs)
        return '{"grade":"correct","feedback":"Accurate paraphrase."}'

    entry = entry_with_senses()
    answer = "Somewhere that holds your money."
    result = asyncio.run(EvaluationProvider(call_llm).evaluate(entry, answer))

    assert result.status is EvaluationStatus.VALID
    assert result.evaluation is not None
    assert result.evaluation.grade is EvaluationGrade.CORRECT
    assert len(calls) == 1
    assert calls[0]["task"] == "vocabulary_answer_evaluation"
    assert calls[0]["max_tokens"] == 500
    assert calls[0]["temperature"] == 0
    assert calls[0]["tools"] == []
    assert json.loads(calls[0]["messages"][1]["content"]) == {
        "display_text": "bank",
        "answer_text": answer,
        "senses": [
            {
                "part_of_speech": "noun",
                "definition": "A financial institution.",
                "example_sentence": "She deposited the cheque at the bank.",
            },
            {
                "part_of_speech": "noun",
                "definition": "Land alongside a river.",
                "example_sentence": "They sat on the grassy bank.",
            },
        ],
    }
    assert answer not in calls[0]["messages"][0]["content"]
    system_rubric = calls[0]["messages"][0]["content"]
    assert "Accept an accurate semantic paraphrase as correct" in system_rubric
    assert "matching any one valid stored sense can be correct" in system_rubric
    assert "Use partial for an incomplete but directionally valid meaning" in system_rubric
    assert "incorrect for an unrelated or wrong meaning" in system_rubric


def test_evaluation_provider_rejects_whitespace_before_calling_provider() -> None:
    calls = 0

    async def call_llm(**kwargs) -> str:
        nonlocal calls
        calls += 1
        return '{"grade":"correct","feedback":"Accurate."}'

    result = asyncio.run(EvaluationProvider(call_llm).evaluate(entry_with_senses(), " \n "))

    assert result.status is EvaluationStatus.INVALID_RESPONSE
    assert result.evaluation is None
    assert calls == 0


@pytest.mark.parametrize("response", ["not json", '{"grade":"unknown","feedback":"No."}'])
def test_evaluation_provider_returns_invalid_response_without_retry(response: str) -> None:
    calls = 0

    async def call_llm(**kwargs) -> str:
        nonlocal calls
        calls += 1
        return response

    result = asyncio.run(EvaluationProvider(call_llm).evaluate(entry_with_senses(), "answer"))

    assert result.status is EvaluationStatus.INVALID_RESPONSE
    assert result.evaluation is None
    assert calls == 1


def test_evaluation_provider_translates_exception_without_retry() -> None:
    calls = 0

    async def call_llm(**kwargs) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    result = asyncio.run(EvaluationProvider(call_llm).evaluate(entry_with_senses(), "answer"))

    assert result.status is EvaluationStatus.PROVIDER_ERROR
    assert result.evaluation is None
    assert calls == 1


class StubEvaluationProvider:
    def __init__(self, result: EvaluationResult) -> None:
        self.result = result
        self.calls: list[tuple[VocabularyEntry, str]] = []

    async def evaluate(
        self, entry: VocabularyEntry, answer_text: str
    ) -> EvaluationResult:
        self.calls.append((entry, answer_text))
        return self.result


def pending_review(tmp_path: Path) -> tuple[ReviewService, Database, int]:
    database = Database(tmp_path / "vocabulary.sqlite3")
    database.initialize()
    CaptureService(database, clock=lambda: NOW).capture_entry(
        "laconic",
        (
            SenseCard(
                "adjective",
                "Using very few words.",
                "His reply was laconic.",
            ),
        ),
    )
    service = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW)
    prepared = service.daily_review()
    assert prepared.event is not None
    return service, database, prepared.event.id


def test_complete_pending_review_persists_semantic_grade_and_raw_answer(
    tmp_path: Path,
) -> None:
    service, database, event_id = pending_review(tmp_path)
    provider = StubEvaluationProvider(
        EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(EvaluationGrade.CORRECT, "Accurate paraphrase."),
        )
    )
    answer = "It means being brief and direct."

    result = asyncio.run(complete_pending_review(service, provider, answer))

    assert result.status is ReviewCompletionStatus.COMPLETED
    assert result.event_id == event_id
    assert result.grade is EvaluationGrade.CORRECT
    assert result.feedback == "Accurate paraphrase."
    assert len(provider.calls) == 1
    assert provider.calls[0][1] == answer
    with database.connect() as connection:
        event = connection.execute(
            "SELECT status, answer_text, grade, evaluation_feedback FROM review_events"
        ).fetchone()
    assert tuple(event) == (
        "answered",
        answer,
        "correct",
        "Accurate paraphrase.",
    )


@pytest.mark.parametrize(
    "status",
    [EvaluationStatus.INVALID_RESPONSE, EvaluationStatus.PROVIDER_ERROR],
)
def test_evaluator_failure_leaves_review_pending(
    tmp_path: Path,
    status: EvaluationStatus,
) -> None:
    service, database, _ = pending_review(tmp_path)
    provider = StubEvaluationProvider(EvaluationResult(status))

    result = asyncio.run(complete_pending_review(service, provider, "my attempt"))

    assert result.status is ReviewCompletionStatus.STORAGE_ERROR
    with database.connect() as connection:
        event = connection.execute(
            "SELECT status, answer_text, grade, evaluation_feedback FROM review_events"
        ).fetchone()
        entry = connection.execute(
            "SELECT review_status, last_reviewed FROM vocabulary_entries"
        ).fetchone()
    assert tuple(event) == ("pending", None, None, None)
    assert tuple(entry) == ("new", None)


def test_show_answer_bypasses_provider_and_persists_deterministic_surrender(
    tmp_path: Path,
) -> None:
    service, database, _ = pending_review(tmp_path)
    provider = StubEvaluationProvider(
        EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(EvaluationGrade.CORRECT, "Should not be used."),
        )
    )

    result = asyncio.run(complete_pending_review(service, provider, "show answer"))

    assert result.status is ReviewCompletionStatus.COMPLETED
    assert result.grade is EvaluationGrade.INCORRECT
    assert result.feedback == SHOW_ANSWER_FEEDBACK
    assert provider.calls == []
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT answer_text, grade, evaluation_feedback FROM review_events"
        ).fetchone()
    assert tuple(stored) == (
        "show answer",
        "incorrect",
        SHOW_ANSWER_FEEDBACK,
    )


@pytest.mark.parametrize("answer", ["answer", " show answer", "show answer "])
def test_only_exact_show_answer_bypasses_provider(
    tmp_path: Path,
    answer: str,
) -> None:
    service, _, _ = pending_review(tmp_path)
    provider = StubEvaluationProvider(
        EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(EvaluationGrade.INCORRECT, "Evaluated normally."),
        )
    )

    result = asyncio.run(complete_pending_review(service, provider, answer))

    assert result.status is ReviewCompletionStatus.COMPLETED
    assert provider.calls[0][1] == answer
    assert result.feedback == "Evaluated normally."


def test_whitespace_answer_is_rejected_without_provider_or_transition(
    tmp_path: Path,
) -> None:
    service, database, _ = pending_review(tmp_path)
    provider = StubEvaluationProvider(
        EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(EvaluationGrade.CORRECT, "Should not be used."),
        )
    )

    result = asyncio.run(complete_pending_review(service, provider, "  \n "))

    assert result.status is ReviewCompletionStatus.INVALID
    assert provider.calls == []
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT status, answer_text FROM review_events"
        ).fetchone()
    assert tuple(stored) == ("pending", None)
