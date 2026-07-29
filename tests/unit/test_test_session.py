from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_vocab.capture import CaptureService
from hermes_vocab.database import Database
from hermes_vocab.models import (
    CardDirection,
    CardScheduleState,
    CaptureCommand,
    CaptureOperation,
    Evaluation,
    EvaluationGrade,
    SenseCard,
    ReviewRating,
    StudyMode,
    StudySessionStatus,
    StudyPromptStatus,
    StudyStartStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService as SessionService
from zoneinfo import ZoneInfo


NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value






































def _setup_v5_service(
    tmp_path: Path,
) -> tuple[SessionService, Database, Clock]:
    database = Database(tmp_path / "v5" / "vocabulary.sqlite3")
    database.initialize()
    clock = Clock(NOW)
    return (
        SessionService(database, ZoneInfo("UTC"), clock=clock),
        database,
        clock,
    )


def _add_directional_cards(
    database: Database,
    index: int,
    *,
    extra_senses: int = 0,
) -> tuple[int, list[int], int, list[int]]:
    captured_at = NOW - timedelta(days=30) + timedelta(minutes=index)
    result = CaptureService(database, clock=lambda: captured_at).capture(
        CaptureCommand(
            display_text=f"term-{index}",
            operation=CaptureOperation.NEW_ENTRY,
            card=SenseCard(
                part_of_speech="noun",
                definition=f"Definition {index}.",
                example_sentence=f"Example {index}.",
            ),
        )
    )
    assert result.entry is not None and result.sense is not None
    sense_ids = [result.sense.id]
    for sense_index in range(extra_senses):
        extra = CaptureService(database, clock=lambda: captured_at).capture(
            CaptureCommand(
                display_text=f"term-{index}",
                operation=CaptureOperation.NEW_SENSE,
                card=SenseCard(
                    part_of_speech="verb",
                    definition=f"Alternate definition {index}-{sense_index}.",
                    example_sentence=f"Alternate example {index}-{sense_index}.",
                ),
            )
        )
        assert extra.sense is not None
        sense_ids.append(extra.sense.id)
    with database.connect() as connection:
        # Capture already creates the directional cards; align their due dates.
        connection.execute(
            """
            UPDATE vocabulary_cards SET due_at = ?, effective_due_at = ?
            WHERE entry_id = ?
            """,
            (_timestamp(NOW), _timestamp(NOW), result.entry.id),
        )
        forward_id = connection.execute(
            """
            SELECT id FROM vocabulary_cards
            WHERE entry_id = ? AND direction = 'forward'
            """,
            (result.entry.id,),
        ).fetchone()["id"]
        reverse_ids = [
            connection.execute(
                """
                SELECT id FROM vocabulary_cards
                WHERE sense_id = ? AND direction = 'reverse'
                """,
                (sense_id,),
            ).fetchone()["id"]
            for sense_id in sense_ids
        ]
        connection.commit()
    assert forward_id is not None and all(card_id is not None for card_id in reverse_ids)
    return result.entry.id, sense_ids, forward_id, reverse_ids


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _make_reviewed(
    database: Database,
    card_id: int,
    *,
    due: datetime,
    stability: float,
    introduced_local_date: str | None = None,
) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE vocabulary_cards
            SET state = 'review', stability = ?, difficulty = 5,
                due_at = ?, effective_due_at = ?, last_review_at = ?,
                repetitions = 1, introduced_local_date = ?
            WHERE id = ?
            """,
            (
                stability,
                _timestamp(due),
                _timestamp(due),
                _timestamp(NOW - timedelta(days=10)),
                introduced_local_date,
                card_id,
            ),
        )
        connection.commit()


def test_v5_forward_test_selects_only_unseen_entries_oldest_first(
    tmp_path: Path,
) -> None:
    service, database, _ = _setup_v5_service(tmp_path)
    cards = [_add_directional_cards(database, index) for index in range(10)]
    _make_reviewed(database, cards[0][2], due=NOW - timedelta(days=2), stability=3)
    _make_reviewed(database, cards[1][2], due=NOW - timedelta(days=1), stability=3)
    _make_reviewed(database, cards[2][2], due=NOW + timedelta(days=5), stability=0.2)
    _make_reviewed(database, cards[3][2], due=NOW + timedelta(days=5), stability=20)

    result = service.start(CardDirection.FORWARD)

    assert result.status is StudyStartStatus.STARTED
    assert result.snapshot is not None
    assert result.snapshot.mode is StudyMode.TEST_FORWARD
    assert [item.card.id for item in result.snapshot.queue] == [
        cards[index][2] for index in range(4, 9)
    ]
    assert all(
        item.card.state is CardScheduleState.NEW for item in result.snapshot.queue
    )
    assert result.snapshot.current_prompt is not None
    assert result.snapshot.current_prompt.status is StudyPromptStatus.PREPARED
    assert result.snapshot.current_prompt.prompt_text == (
        "Question 1 of 5\nWhat does 'term-4' mean?"
    )


def test_v5_reverse_test_uses_exact_sense_card_and_definition_only(
    tmp_path: Path,
) -> None:
    service, database, _ = _setup_v5_service(tmp_path)
    cards = [_add_directional_cards(database, index) for index in range(5)]

    result = service.start(CardDirection.REVERSE)

    assert result.status is StudyStartStatus.STARTED
    assert result.snapshot is not None
    first = result.snapshot.queue[0].card
    assert first.direction is CardDirection.REVERSE
    assert first.sense_id == cards[0][1][0]
    prompt = result.snapshot.current_prompt
    assert prompt is not None
    assert prompt.prompt_text == (
        "Question 1 of 5\nWhich saved word matches this definition?\n"
        "Definition 0."
    )
    assert "term-0" not in prompt.prompt_text
    assert "Example 0." not in prompt.prompt_text


@pytest.mark.parametrize("direction", list(CardDirection))
def test_v5_test_requires_five_distinct_entries_without_partial_mutation(
    tmp_path: Path,
    direction: CardDirection,
) -> None:
    service, database, _ = _setup_v5_service(tmp_path)
    for index in range(4):
        _add_directional_cards(database, index)
    result = service.start(direction)
    assert result.status is StudyStartStatus.EMPTY
    assert result.available_count == 4
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM study_queue").fetchone()[0] == 0



def test_v5_matching_test_resumes_and_other_modes_conflict_without_replacement(
    tmp_path: Path,
) -> None:
    service, database, _ = _setup_v5_service(tmp_path)
    for index in range(5):
        _add_directional_cards(database, index)
    started = service.start(CardDirection.FORWARD)

    resumed = SessionService(
        database, ZoneInfo("UTC"), clock=lambda: NOW + timedelta(minutes=1)
    ).start(CardDirection.FORWARD)
    opposite = service.start(CardDirection.REVERSE)
    review = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW).start()

    assert resumed.status is StudyStartStatus.RESUMED
    assert resumed.snapshot == started.snapshot
    assert opposite.status is StudyStartStatus.CONFLICT
    assert review.status is StudyStartStatus.CONFLICT
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_sessions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT mode FROM study_sessions"
        ).fetchone()[0] == "test_forward"


def test_v5_active_review_blocks_both_test_modes_without_replacement(
    tmp_path: Path,
) -> None:
    service, database, _ = _setup_v5_service(tmp_path)
    for index in range(5):
        _add_directional_cards(database, index)
    review = ReviewService(database, ZoneInfo("UTC"), clock=lambda: NOW).start()
    assert review.status is StudyStartStatus.STARTED

    assert service.start(CardDirection.FORWARD).status is StudyStartStatus.CONFLICT
    assert service.start(CardDirection.REVERSE).status is StudyStartStatus.CONFLICT
    with database.connect() as connection:
        assert connection.execute(
            "SELECT mode FROM study_sessions"
        ).fetchone()[0] == "review"


def test_v5_restart_preserves_original_queue_and_answered_rating_phase(
    tmp_path: Path,
) -> None:
    service, database, clock = _setup_v5_service(tmp_path)
    for index in range(5):
        _add_directional_cards(database, index)
    started = service.start(CardDirection.FORWARD)
    assert started.snapshot is not None and started.snapshot.current_prompt is not None
    original_ids = tuple(item.id for item in started.snapshot.queue)
    prompt = started.snapshot.current_prompt
    assert service.study.record_delivery(
        prompt.id,
        delivery_id="delivered",
        content_fingerprint="fingerprint",
    ) is not None
    clock.value = NOW + timedelta(minutes=3)
    assert service.study.record_answer(
        prompt.id,
        "partial answer",
        Evaluation(EvaluationGrade.PARTIAL, "Missing one detail."),
    ) is not None

    restarted = SessionService(database, ZoneInfo("UTC"), clock=clock).start(
        CardDirection.FORWARD
    )

    assert restarted.status is StudyStartStatus.RESUMED
    assert restarted.snapshot is not None
    assert tuple(item.id for item in restarted.snapshot.queue) == original_ids
    assert restarted.snapshot.current_prompt is not None
    assert restarted.snapshot.current_prompt.id == prompt.id
    assert restarted.snapshot.current_prompt.status is StudyPromptStatus.ANSWERED


def test_v5_new_test_card_enters_review_schedule_without_mutating_siblings(
    tmp_path: Path,
) -> None:
    service, database, clock = _setup_v5_service(tmp_path)
    for index in range(5):
        _add_directional_cards(database, index)
    started = service.start(CardDirection.FORWARD)
    assert started.snapshot is not None
    first_card_id = started.snapshot.queue[0].card.id
    with database.connect() as connection:
        before = {
            row["id"]: tuple(row)[1:]
            for row in connection.execute(
                """
                SELECT id, state, stability, difficulty, due_at,
                       effective_due_at, last_review_at, repetitions, lapses
                FROM vocabulary_cards ORDER BY id
                """
            )
        }

    _complete_current(
        service,
        clock,
        grade=EvaluationGrade.CORRECT,
        rating=ReviewRating.GOOD,
        index=1,
    )

    with database.connect() as connection:
        after = {
            row["id"]: tuple(row)[1:]
            for row in connection.execute(
                """
                SELECT id, state, stability, difficulty, due_at,
                       effective_due_at, last_review_at, repetitions, lapses
                FROM vocabulary_cards ORDER BY id
                """
            )
        }
    assert after[first_card_id] != before[first_card_id]
    assert {
        card_id: state for card_id, state in after.items() if card_id != first_card_id
    } == {
        card_id: state for card_id, state in before.items() if card_id != first_card_id
    }


def test_v5_test_selection_bypasses_daily_new_quota_for_explicit_introduction(
    tmp_path: Path,
) -> None:
    service, database, _ = _setup_v5_service(tmp_path)
    cards = [_add_directional_cards(database, index) for index in range(10)]
    for index in range(5):
        _make_reviewed(
            database,
            cards[index][3][0],
            due=NOW + timedelta(days=5),
            stability=float(index + 1),
            introduced_local_date=NOW.date().isoformat(),
        )

    result = service.start(CardDirection.REVERSE)

    assert result.status is StudyStartStatus.STARTED
    assert result.snapshot is not None
    assert [item.card.id for item in result.snapshot.queue] == [
        cards[index][3][0] for index in range(5, 10)
    ]
    assert all(
        item.card.state is CardScheduleState.NEW for item in result.snapshot.queue
    )


def test_v5_concurrent_matching_starts_create_one_original_five(
    tmp_path: Path,
) -> None:
    service, database, _ = _setup_v5_service(tmp_path)
    for index in range(5):
        _add_directional_cards(database, index)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: service.start(CardDirection.FORWARD),
                range(2),
            )
        )

    assert {result.status for result in results} == {
        StudyStartStatus.STARTED,
        StudyStartStatus.RESUMED,
    }
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM study_queue").fetchone()[0] == 5


def _complete_current(
    service: SessionService,
    clock: Clock,
    *,
    grade: EvaluationGrade,
    rating: ReviewRating,
    index: int,
) -> None:
    snapshot = service.study.snapshot()
    assert snapshot is not None and snapshot.current_prompt is not None
    prompt = snapshot.current_prompt
    clock.value = NOW + timedelta(minutes=index)
    assert service.study.record_delivery(
        prompt.id,
        delivery_id=f"delivery-{index}",
        content_fingerprint=f"fingerprint-{index}",
    ) is not None
    assert service.study.record_answer(
        prompt.id,
        "submitted answer",
        Evaluation(grade, f"Feedback {index}."),
    ) is not None
    result = service.study.finalize(prompt.id, rating)
    assert result.status.value == "completed"
    if result.snapshot is not None and result.snapshot.status is StudySessionStatus.ACTIVE:
        assert service.prepare_current_prompt() is not None


def test_v5_reverse_test_updates_only_exact_five_sense_schedules_at_actual_times(
    tmp_path: Path,
) -> None:
    service, database, clock = _setup_v5_service(tmp_path)
    for index in range(5):
        _add_directional_cards(database, index)
    started = service.start(CardDirection.REVERSE)
    assert started.snapshot is not None

    for index in range(1, 6):
        _complete_current(
            service,
            clock,
            grade=EvaluationGrade.CORRECT,
            rating=ReviewRating.GOOD,
            index=index,
        )

    summary = service.summary(started.snapshot.session_id)
    assert summary is not None
    assert (summary.correct, summary.partial, summary.incorrect) == (5, 0, 0)
    with database.connect() as connection:
        reverse_rows = connection.execute(
            """
            SELECT repetitions, last_review_at FROM vocabulary_cards
            WHERE direction = 'reverse' ORDER BY id
            """
        ).fetchall()
        forward_repetitions = connection.execute(
            """
            SELECT repetitions FROM vocabulary_cards
            WHERE direction = 'forward' ORDER BY id
            """
        ).fetchall()
    assert [row["repetitions"] for row in reverse_rows] == [1] * 5
    assert [row["last_review_at"] for row in reverse_rows] == [
        _timestamp(NOW + timedelta(minutes=index)) for index in range(1, 6)
    ]
    assert [row["repetitions"] for row in forward_repetitions] == [0] * 5


def test_v5_again_retry_never_changes_original_five_correctness_denominator(
    tmp_path: Path,
) -> None:
    service, database, clock = _setup_v5_service(tmp_path)
    for index in range(5):
        _add_directional_cards(database, index)
    started = service.start(CardDirection.FORWARD)
    assert started.snapshot is not None

    _complete_current(
        service,
        clock,
        grade=EvaluationGrade.INCORRECT,
        rating=ReviewRating.AGAIN,
        index=1,
    )
    for index in range(2, 6):
        _complete_current(
            service,
            clock,
            grade=EvaluationGrade.CORRECT,
            rating=ReviewRating.GOOD,
            index=index,
        )

    before_retry = service.summary(started.snapshot.session_id)
    assert before_retry is not None
    assert (before_retry.correct, before_retry.partial, before_retry.incorrect) == (
        4,
        0,
        1,
    )
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_queue").fetchone()[0] == 6

    retry_snapshot = service.study.snapshot()
    assert retry_snapshot is not None and retry_snapshot.current_prompt is not None
    assert retry_snapshot.progress.completed == 5
    assert retry_snapshot.progress.total == 5
    assert retry_snapshot.current_prompt.prompt_text.startswith(
        "Question 5 of 5 · retry\n"
    )

    _complete_current(
        service,
        clock,
        grade=EvaluationGrade.INCORRECT,
        rating=ReviewRating.AGAIN,
        index=6,
    )
    after_retry = service.summary(started.snapshot.session_id)
    assert after_retry == before_retry
