from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hermes_vocab.database import Database

from hermes_vocab.hermes_plugin.evaluation import (
    MAX_EVALUATION_FEEDBACK_LENGTH,
    EvaluationProvider,
    EvaluationResult,
    EvaluationStatus,
    allowed_ratings,
    continue_study_answer,
    normalize_reverse_answer,
    parse_evaluation_response,
    parse_rating,
)
from hermes_vocab.models import (
    CardDirection,
    Evaluation,
    EvaluationGrade,
    FinalizeStatus,
    PARAMETER_FINGERPRINT,
    PARAMETERS_VERSION,
    ReviewRating,
    SCHEDULER_KIND,
    SCHEDULER_VERSION,
    StudyAnswerStatus,
    StudyPromptStatus,
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
        await asyncio.sleep(0)
        return self.result


def timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def answerable_study(
    tmp_path: Path,
    *,
    direction: CardDirection = CardDirection.FORWARD,
) -> tuple[ReviewService, Database, int]:
    database = Database(tmp_path / "vocabulary.sqlite3")
    database.initialize()
    sense_id = 12 if direction is CardDirection.REVERSE else None
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO vocabulary_entries (
                id, display_text, normalized_text, date_added,
                last_reviewed, review_status
            ) VALUES (1, 'Pro Forma', 'pro forma', ?, NULL, 'new')
            """,
            (timestamp(NOW - timedelta(days=20)),),
        )
        connection.executemany(
            """
            INSERT INTO vocabulary_senses (
                id, entry_id, definition, part_of_speech,
                example_sentence, source_context, date_added
            ) VALUES (?, 1, ?, 'phrase', ?, NULL, ?)
            """,
            (
                (
                    11,
                    "Done as a matter of form or convention.",
                    "The board made a pro forma approval.",
                    timestamp(NOW - timedelta(days=20)),
                ),
                (
                    12,
                    "A projected financial statement.",
                    "The pro forma showed next year's revenue.",
                    timestamp(NOW - timedelta(days=19)),
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO vocabulary_cards (
                id, entry_id, sense_id, direction, state, stability,
                difficulty, due_at, effective_due_at, last_review_at,
                repetitions, lapses, scheduler_kind, scheduler_version,
                parameters_version, parameter_fingerprint,
                desired_retention, created_at
            ) VALUES (
                101, 1, ?, ?, 'new', NULL, NULL, ?, ?, NULL,
                0, 0, ?, ?, ?, ?, 0.9, ?
            )
            """,
            (
                sense_id,
                direction.value,
                timestamp(NOW - timedelta(days=1)),
                timestamp(NOW - timedelta(days=1)),
                SCHEDULER_KIND,
                SCHEDULER_VERSION,
                PARAMETERS_VERSION,
                PARAMETER_FINGERPRINT,
                timestamp(NOW - timedelta(days=20)),
            ),
        )
        connection.commit()
    service = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW)
    started = service.start()
    assert started.snapshot is not None
    prompt = service.prepare_current_prompt(
        f"{direction.value}-prompt",
        "Persisted prompt text.",
    )
    assert prompt is not None
    delivered = service.record_delivery(
        prompt.id,
        delivery_id=f"{direction.value}-delivery",
        content_fingerprint=f"{direction.value}-fingerprint",
    )
    assert delivered is not None
    assert delivered.status is StudyPromptStatus.DELIVERED
    return service, database, prompt.id


def valid_provider(grade: EvaluationGrade) -> StubEvaluationProvider:
    return StubEvaluationProvider(
        EvaluationResult(
            EvaluationStatus.VALID,
            Evaluation(grade, f"{grade.value.title()} feedback."),
        )
    )


def test_partial_forward_persists_one_draft_and_waits_for_allowed_rating(
    tmp_path: Path,
) -> None:
    service, database, prompt_id = answerable_study(tmp_path)
    provider = valid_provider(EvaluationGrade.PARTIAL)
    answer = "It is a projected statement."

    evaluated = asyncio.run(continue_study_answer(service, provider, answer))

    assert evaluated.status is StudyAnswerStatus.AWAITING_RATING
    assert evaluated.allowed_ratings == (ReviewRating.AGAIN, ReviewRating.HARD)
    assert len(provider.calls) == 1
    with database.connect() as connection:
        assert tuple(
            connection.execute(
                """
                SELECT submitted_answer, evaluator_grade, evaluation_feedback
                FROM answer_drafts WHERE prompt_id = ?
                """,
                (prompt_id,),
            ).fetchone()
        ) == (answer, "partial", "Partial feedback.")
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT repetitions FROM vocabulary_cards WHERE id = 101"
        ).fetchone()[0] == 0

    invalid = asyncio.run(continue_study_answer(service, provider, "good"))
    finalized = asyncio.run(continue_study_answer(service, provider, " HARD "))

    assert invalid.status is StudyAnswerStatus.INVALID_RATING
    assert invalid.allowed_ratings == (ReviewRating.AGAIN, ReviewRating.HARD)
    assert finalized.status is StudyAnswerStatus.FINALIZED
    assert finalized.finalization.status is FinalizeStatus.COMPLETED
    assert len(provider.calls) == 1
    with database.connect() as connection:
        assert connection.execute(
            "SELECT rating FROM review_attempts"
        ).fetchone()[0] == "hard"


def test_correct_forward_restart_and_invalid_rating_never_re_evaluate(
    tmp_path: Path,
) -> None:
    service, database, _ = answerable_study(tmp_path)
    provider = valid_provider(EvaluationGrade.CORRECT)
    first = asyncio.run(
        continue_study_answer(service, provider, "A conventional projected statement.")
    )
    restarted = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW)

    invalid = asyncio.run(continue_study_answer(restarted, provider, "again"))
    completed = asyncio.run(continue_study_answer(restarted, provider, "easy"))

    assert first.allowed_ratings == (
        ReviewRating.HARD,
        ReviewRating.GOOD,
        ReviewRating.EASY,
    )
    assert invalid.status is StudyAnswerStatus.INVALID_RATING
    assert completed.status is StudyAnswerStatus.FINALIZED
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("answer", "provider_grade", "expected_calls"),
    [
        ("wrong meaning", EvaluationGrade.INCORRECT, 1),
        ("show answer", EvaluationGrade.CORRECT, 0),
        (" IDK ", EvaluationGrade.CORRECT, 0),
    ],
)
def test_incorrect_and_surrender_auto_finalize_again_and_append_retry(
    tmp_path: Path,
    answer: str,
    provider_grade: EvaluationGrade,
    expected_calls: int,
) -> None:
    service, database, _ = answerable_study(tmp_path)
    provider = valid_provider(provider_grade)

    result = asyncio.run(continue_study_answer(service, provider, answer))

    assert result.status is StudyAnswerStatus.FINALIZED
    assert result.finalization.transition.retry_same_session is True
    assert len(provider.calls) == expected_calls
    with database.connect() as connection:
        assert connection.execute(
            "SELECT rating FROM review_attempts"
        ).fetchone()[0] == "again"
        assert connection.execute(
            "SELECT COUNT(*) FROM study_queue WHERE retry_of_queue_item_id IS NOT NULL"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("answer", [" show answer", "show answer "])
def test_only_exact_show_answer_bypasses_forward_provider(
    tmp_path: Path,
    answer: str,
) -> None:
    service, _, _ = answerable_study(tmp_path)
    provider = valid_provider(EvaluationGrade.CORRECT)

    result = asyncio.run(continue_study_answer(service, provider, answer))

    assert result.status is StudyAnswerStatus.AWAITING_RATING
    assert len(provider.calls) == 1
    assert provider.calls[0][1] == answer


@pytest.mark.parametrize(
    ("answer", "expected_grade"),
    [
        (" pro   forma. ", EvaluationGrade.CORRECT),
        ("PRO FORMA!!!", EvaluationGrade.CORRECT),
        ("pro-forma", EvaluationGrade.CORRECT),
        ("proforma", EvaluationGrade.CORRECT),
        ("pro/forma", EvaluationGrade.CORRECT),
        ("pro form", EvaluationGrade.INCORRECT),
        ("projected statement", EvaluationGrade.INCORRECT),
        ("obdurate", EvaluationGrade.INCORRECT),
    ],
)
def test_reverse_answer_is_normalized_exactly_without_model(
    tmp_path: Path,
    answer: str,
    expected_grade: EvaluationGrade,
) -> None:
    service, _, _ = answerable_study(tmp_path, direction=CardDirection.REVERSE)
    provider = valid_provider(EvaluationGrade.CORRECT)

    result = asyncio.run(continue_study_answer(service, provider, answer))

    assert result.context.draft.evaluation.grade is expected_grade
    assert result.context.sense is not None
    assert result.context.sense.id == 12
    assert len(result.context.entry.senses) == 2
    assert provider.calls == []
    assert result.status is (
        StudyAnswerStatus.AWAITING_RATING
        if expected_grade is EvaluationGrade.CORRECT
        else StudyAnswerStatus.FINALIZED
    )


def test_reverse_normalizer_and_rating_parser_are_state_deterministic() -> None:
    assert normalize_reverse_answer("  Pro-forma...  ") == "proforma"
    assert normalize_reverse_answer("can't") == normalize_reverse_answer("cant")
    assert normalize_reverse_answer("C++") == normalize_reverse_answer("C")
    assert parse_rating(
        " GOOD ",
        (ReviewRating.HARD, ReviewRating.GOOD, ReviewRating.EASY),
    ) is ReviewRating.GOOD
    assert parse_rating(
        "good",
        (ReviewRating.AGAIN, ReviewRating.HARD),
    ) is None
    assert allowed_ratings(EvaluationGrade.INCORRECT) == ()


@pytest.mark.parametrize(
    "provider_status",
    [EvaluationStatus.INVALID_RESPONSE, EvaluationStatus.PROVIDER_ERROR],
)
def test_provider_failure_keeps_same_answerable_prompt_and_writes_nothing(
    tmp_path: Path,
    provider_status: EvaluationStatus,
) -> None:
    service, database, prompt_id = answerable_study(tmp_path)
    provider = StubEvaluationProvider(EvaluationResult(provider_status))

    result = asyncio.run(continue_study_answer(service, provider, "my answer"))

    assert result.status is StudyAnswerStatus.EVALUATION_ERROR
    assert service.answerable_prompt().id == prompt_id
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT repetitions FROM vocabulary_cards WHERE id = 101"
        ).fetchone()[0] == 0


def test_rating_persistence_failure_retains_draft_without_duplicate_evaluation(
    tmp_path: Path,
) -> None:
    service, database, _ = answerable_study(tmp_path)
    provider = valid_provider(EvaluationGrade.PARTIAL)
    awaiting = asyncio.run(continue_study_answer(service, provider, "partial answer"))
    assert awaiting.status is StudyAnswerStatus.AWAITING_RATING
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_u5_attempt
            BEFORE INSERT ON review_attempts
            BEGIN SELECT RAISE(ABORT, 'injected U5 attempt failure'); END
            """
        )
        connection.commit()

    failed = asyncio.run(continue_study_answer(service, provider, "hard"))

    assert failed.status is StudyAnswerStatus.STORAGE_ERROR
    assert failed.allowed_ratings == (ReviewRating.AGAIN, ReviewRating.HARD)
    assert len(provider.calls) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_u5_attempt")
        connection.commit()

    retried = asyncio.run(continue_study_answer(service, provider, "hard"))
    assert retried.status is StudyAnswerStatus.FINALIZED
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("answer", "provider_grade", "expected_calls"),
    [
        ("wrong meaning", EvaluationGrade.INCORRECT, 1),
        ("show answer", EvaluationGrade.CORRECT, 0),
        ("idk", EvaluationGrade.CORRECT, 0),
    ],
)
def test_auto_again_persistence_failure_retries_from_draft_without_re_evaluation(
    tmp_path: Path,
    answer: str,
    provider_grade: EvaluationGrade,
    expected_calls: int,
) -> None:
    service, database, _ = answerable_study(tmp_path)
    provider = valid_provider(provider_grade)
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_auto_again_attempt
            BEFORE INSERT ON review_attempts
            BEGIN SELECT RAISE(ABORT, 'injected auto Again failure'); END
            """
        )
        connection.commit()

    failed = asyncio.run(continue_study_answer(service, provider, answer))

    assert failed.status is StudyAnswerStatus.STORAGE_ERROR
    assert failed.context.draft.evaluation.grade is EvaluationGrade.INCORRECT
    assert failed.allowed_ratings == ()
    assert len(provider.calls) == expected_calls
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_auto_again_attempt")
        connection.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        retried = list(
            executor.map(
                lambda _: asyncio.run(
                    continue_study_answer(service, provider, "not a rating")
                ),
                range(2),
            )
        )

    assert StudyAnswerStatus.FINALIZED in {result.status for result in retried}
    assert {result.status for result in retried} <= {
        StudyAnswerStatus.FINALIZED,
        StudyAnswerStatus.STALE,
        StudyAnswerStatus.NO_ACTIVE,
    }
    assert len(provider.calls) == expected_calls
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT rating FROM review_attempts").fetchone()[0] == "again"


def test_concurrent_answers_and_ratings_persist_one_draft_and_attempt(
    tmp_path: Path,
) -> None:
    service, database, _ = answerable_study(tmp_path)
    provider = valid_provider(EvaluationGrade.CORRECT)

    async def answer_twice():
        return await asyncio.gather(
            continue_study_answer(service, provider, "first answer"),
            continue_study_answer(service, provider, "second answer"),
        )

    asyncio.run(answer_twice())
    restarted = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as executor:
        ratings = list(
            executor.map(
                lambda _: asyncio.run(
                    continue_study_answer(restarted, provider, "good")
                ),
                range(2),
            )
        )

    assert StudyAnswerStatus.FINALIZED in {result.status for result in ratings}
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 1
