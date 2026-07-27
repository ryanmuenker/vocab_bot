from __future__ import annotations

import json

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hermes_vocab.models import CardSchedule, CardScheduleState, ReviewRating
from hermes_vocab.scheduling import (
    DEFAULT_PARAMETERS,
    DESIRED_RETENTION,
    MAXIMUM_INTERVAL_DAYS,
    PARAMETER_FINGERPRINT,
    PARAMETERS_VERSION,
    SCHEDULER_KIND,
    SCHEDULER_VERSION,
    ScheduleTransition,
    retrievability,
    transition,
)

NOW = datetime(2026, 7, 17, 12, 30, tzinfo=UTC)


def new_card() -> CardSchedule:
    return CardSchedule(state=CardScheduleState.NEW, due=NOW)


def card_payload(card: CardSchedule) -> dict[str, str | float | int | None]:
    return {
        "state": card.state.value,
        "due": card.due.isoformat(),
        "stability": card.stability,
        "difficulty": card.difficulty,
        "last_review": card.last_review.isoformat() if card.last_review else None,
        "repetitions": card.repetitions,
        "lapses": card.lapses,
        "scheduler_kind": card.scheduler_kind,
        "scheduler_version": card.scheduler_version,
        "parameters_version": card.parameters_version,
        "parameter_fingerprint": card.parameter_fingerprint,
        "desired_retention": card.desired_retention,
    }


def card_from_payload(payload: dict[str, object]) -> CardSchedule:
    last_review = payload["last_review"]
    assert last_review is None or isinstance(last_review, str)
    return CardSchedule(
        state=CardScheduleState(str(payload["state"])),
        due=datetime.fromisoformat(str(payload["due"])),
        stability=(
            float(payload["stability"]) if payload["stability"] is not None else None
        ),
        difficulty=(
            float(payload["difficulty"]) if payload["difficulty"] is not None else None
        ),
        last_review=datetime.fromisoformat(last_review) if last_review else None,
        repetitions=int(str(payload["repetitions"])),
        lapses=int(str(payload["lapses"])),
        scheduler_kind=str(payload["scheduler_kind"]),
        scheduler_version=str(payload["scheduler_version"]),
        parameters_version=str(payload["parameters_version"]),
        parameter_fingerprint=str(payload["parameter_fingerprint"]),
        desired_retention=float(payload["desired_retention"]),
    )


@pytest.mark.parametrize(
    ("rating", "stability", "difficulty", "interval_days", "state"),
    [
        (ReviewRating.AGAIN, 0.212, 6.4133, 1, CardScheduleState.RELEARNING),
        (
            ReviewRating.HARD,
            1.2931,
            5.112170705601055,
            1,
            CardScheduleState.REVIEW,
        ),
        (
            ReviewRating.GOOD,
            2.3065,
            2.118103970459015,
            2,
            CardScheduleState.REVIEW,
        ),
        (ReviewRating.EASY, 8.2956, 1.0, 8, CardScheduleState.REVIEW),
    ],
)
def test_first_transition_matches_pinned_py_fsrs_631_golden_values(
    rating: ReviewRating,
    stability: float,
    difficulty: float,
    interval_days: int,
    state: CardScheduleState,
) -> None:
    result = transition(new_card(), rating, NOW)

    assert result.before == new_card()
    assert result.after.state is state
    assert result.after.stability == pytest.approx(stability)
    assert result.after.difficulty == pytest.approx(difficulty)
    assert result.after.due == NOW + timedelta(days=interval_days)
    assert result.after.last_review == NOW
    assert result.after.repetitions == 1
    assert result.after.lapses == 0
    assert result.raw_due == result.after.due


def test_overdue_review_uses_lower_retrievability_and_changes_transition() -> None:
    card = transition(new_card(), ReviewRating.GOOD, NOW).after
    on_time = card.due
    overdue = NOW + timedelta(days=20)

    on_time_retrievability = retrievability(card, on_time)
    overdue_retrievability = retrievability(card, overdue)
    on_time_result = transition(card, ReviewRating.GOOD, on_time)
    overdue_result = transition(card, ReviewRating.GOOD, overdue)

    assert overdue_retrievability < on_time_retrievability
    assert overdue_result.retrievability == pytest.approx(overdue_retrievability)
    assert overdue_result.after.stability > on_time_result.after.stability
    assert overdue_result.after.due > on_time_result.after.due


def test_later_review_transition_matches_py_fsrs_631_golden_values() -> None:
    card = transition(new_card(), ReviewRating.GOOD, NOW).after
    reviewed_at = NOW + timedelta(days=20)

    result = transition(card, ReviewRating.GOOD, reviewed_at)

    assert result.after.state is CardScheduleState.REVIEW
    assert result.after.stability == pytest.approx(
        32.78537806272411, rel=1e-12, abs=1e-12
    )
    assert result.after.difficulty == pytest.approx(
        2.1112142357853942, rel=1e-12, abs=1e-12
    )
    assert result.after.due == datetime(2026, 9, 8, 12, 30, tzinfo=UTC)


def test_zero_elapsed_early_review_is_safe_and_deterministic() -> None:
    card = transition(new_card(), ReviewRating.GOOD, NOW).after

    first = transition(card, ReviewRating.GOOD, NOW)
    second = transition(card, ReviewRating.GOOD, NOW)

    assert first == second
    assert first.retrievability == pytest.approx(1.0)
    assert first.after.stability is not None
    assert first.after.stability >= card.stability


@pytest.mark.parametrize(
    "invalid_instant",
    [
        datetime(2026, 7, 17, 12, 30),
        datetime(2026, 7, 17, 8, 30, tzinfo=ZoneInfo("America/New_York")),
    ],
)
def test_review_operations_reject_naive_and_non_utc_instants_without_mutation(
    invalid_instant: datetime,
) -> None:
    card = transition(new_card(), ReviewRating.GOOD, NOW).after

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        transition(card, ReviewRating.GOOD, invalid_instant)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        retrievability(card, invalid_instant)

    assert card.last_review == NOW
    assert card.repetitions == 1


def test_reviewed_schedule_requires_last_review() -> None:
    with pytest.raises(ValueError, match="last_review"):
        CardSchedule(
            state=CardScheduleState.REVIEW,
            stability=2.0,
            difficulty=5.0,
            due=NOW,
            repetitions=1,
        )


def test_reviewed_schedule_requires_at_least_one_repetition() -> None:
    with pytest.raises(ValueError, match="at least one repetition"):
        CardSchedule(
            state=CardScheduleState.REVIEW,
            stability=2.0,
            difficulty=5.0,
            due=NOW,
            last_review=NOW,
        )


@pytest.mark.parametrize(
    "stability",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_schedule_rejects_non_positive_or_non_finite_stability(
    stability: float,
) -> None:
    with pytest.raises(ValueError, match="stability"):
        CardSchedule(
            state=CardScheduleState.REVIEW,
            stability=stability,
            difficulty=5.0,
            due=NOW,
            last_review=NOW,
            repetitions=1,
        )


@pytest.mark.parametrize(
    "difficulty",
    [0.0, 10.1, float("nan"), float("inf"), float("-inf")],
)
def test_schedule_rejects_non_finite_or_out_of_range_difficulty(
    difficulty: float,
) -> None:
    with pytest.raises(ValueError, match="difficulty"):
        CardSchedule(
            state=CardScheduleState.REVIEW,
            stability=2.0,
            difficulty=difficulty,
            due=NOW,
            last_review=NOW,
            repetitions=1,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"stability": 2.0, "difficulty": 5.0},
        {"last_review": NOW},
        {"repetitions": 1},
        {"lapses": 1},
    ],
)
def test_new_schedule_rejects_review_state(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="new schedules"):
        CardSchedule(state=CardScheduleState.NEW, due=NOW, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("repetitions", "lapses"),
    [(-1, 0), (0, -1), pytest.param(1, 2, id="lapses-exceed-repetitions")],
)
def test_schedule_rejects_invalid_counters(repetitions: int, lapses: int) -> None:
    with pytest.raises(ValueError, match="counters"):
        CardSchedule(
            state=CardScheduleState.REVIEW,
            stability=2.0,
            difficulty=5.0,
            due=NOW,
            last_review=NOW,
            repetitions=repetitions,
            lapses=lapses,
        )


def test_schedule_is_immutable_and_metadata_survives_transition() -> None:
    card = new_card()

    with pytest.raises(FrozenInstanceError):
        card.repetitions = 9  # type: ignore[misc]

    result = transition(card, ReviewRating.HARD, NOW)

    assert DEFAULT_PARAMETERS == (
        0.212,
        1.2931,
        2.3065,
        8.2956,
        6.4133,
        0.8334,
        3.0194,
        0.001,
        1.8722,
        0.1666,
        0.796,
        1.4835,
        0.0614,
        0.2629,
        1.6483,
        0.6014,
        1.8729,
        0.5425,
        0.0912,
        0.0658,
        0.1542,
    )
    assert DESIRED_RETENTION == 0.90
    assert MAXIMUM_INTERVAL_DAYS == 3650
    assert result.before.scheduler_version == SCHEDULER_VERSION
    assert result.after.scheduler_version == SCHEDULER_VERSION
    assert result.before.parameters_version == PARAMETERS_VERSION
    assert result.after.parameters_version == PARAMETERS_VERSION
    assert result.before.scheduler_kind == SCHEDULER_KIND
    assert result.after.scheduler_kind == SCHEDULER_KIND
    assert result.before.parameter_fingerprint == PARAMETER_FINGERPRINT
    assert result.after.parameter_fingerprint == PARAMETER_FINGERPRINT
    assert result.before.desired_retention == DESIRED_RETENTION
    assert result.after.desired_retention == DESIRED_RETENTION


def test_first_again_marks_one_same_session_retry() -> None:
    result = transition(new_card(), ReviewRating.AGAIN, NOW)

    assert result.retry_same_session is True
    assert result.effective_due == result.raw_due
    assert result.after.state is CardScheduleState.RELEARNING


def test_second_same_session_again_retains_raw_state_and_applies_due_floor() -> None:
    first = transition(new_card(), ReviewRating.AGAIN, NOW)
    due_floor = NOW + timedelta(days=2)

    second = transition(
        first.after,
        ReviewRating.AGAIN,
        NOW,
        same_session_retry=True,
        due_floor_utc=due_floor,
    )

    assert second.retry_same_session is False
    assert second.after.state is CardScheduleState.RELEARNING
    assert second.after.due == second.raw_due
    assert second.raw_due < due_floor
    assert second.effective_due == due_floor
    assert second.after.repetitions == 2
    assert second.after.lapses == 1


def test_later_relearning_transition_matches_py_fsrs_631_golden_values() -> None:
    first = transition(new_card(), ReviewRating.AGAIN, NOW)

    result = transition(
        first.after,
        ReviewRating.AGAIN,
        NOW,
        same_session_retry=True,
        due_floor_utc=NOW + timedelta(days=2),
    )

    assert result.after.state is CardScheduleState.RELEARNING
    assert result.after.stability == pytest.approx(
        0.08335671711031604, rel=1e-12, abs=1e-12
    )
    assert result.after.difficulty == pytest.approx(
        8.806304468856837, rel=1e-12, abs=1e-12
    )
    assert result.raw_due == NOW + timedelta(days=1)


def test_scalar_schedule_and_transition_metadata_round_trip_through_json() -> None:
    first = transition(new_card(), ReviewRating.AGAIN, NOW)
    original = transition(
        first.after,
        ReviewRating.AGAIN,
        NOW,
        same_session_retry=True,
        due_floor_utc=NOW + timedelta(days=2),
    )
    serialized = json.dumps(
        {
            "before": card_payload(original.before),
            "after": card_payload(original.after),
            "rating": original.rating.value,
            "reviewed_at": original.reviewed_at.isoformat(),
            "retrievability": original.retrievability,
            "raw_due": original.raw_due.isoformat(),
            "effective_due": original.effective_due.isoformat(),
            "retry_same_session": original.retry_same_session,
        },
        sort_keys=True,
    )

    payload = json.loads(serialized)
    restored = ScheduleTransition(
        before=card_from_payload(payload["before"]),
        after=card_from_payload(payload["after"]),
        rating=ReviewRating(payload["rating"]),
        reviewed_at=datetime.fromisoformat(payload["reviewed_at"]),
        retrievability=float(payload["retrievability"]),
        raw_due=datetime.fromisoformat(payload["raw_due"]),
        effective_due=datetime.fromisoformat(payload["effective_due"]),
        retry_same_session=bool(payload["retry_same_session"]),
    )

    assert restored == original
    assert restored.before.scheduler_kind == SCHEDULER_KIND
    assert restored.after.scheduler_version == SCHEDULER_VERSION
    assert restored.after.parameter_fingerprint == PARAMETER_FINGERPRINT
    assert restored.after.desired_retention == DESIRED_RETENTION


def test_second_again_requires_an_explicit_utc_floor() -> None:
    first = transition(new_card(), ReviewRating.AGAIN, NOW)

    with pytest.raises(ValueError, match="due_floor_utc"):
        transition(
            first.after,
            ReviewRating.AGAIN,
            NOW,
            same_session_retry=True,
        )

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        transition(
            first.after,
            ReviewRating.AGAIN,
            NOW,
            same_session_retry=True,
            due_floor_utc=datetime(2026, 7, 18),
        )
