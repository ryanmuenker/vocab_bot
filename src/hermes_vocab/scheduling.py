"""Deterministic, dependency-free FSRS-6 scheduling.

The equations and default weights are independently expressed from the published
FSRS-6 memory model and py-fsrs 6.3.1. py-fsrs is Copyright (c) Jarrett Ye and
contributors and is available under the MIT License:
https://github.com/open-spaced-repetition/py-fsrs/tree/v6.3.1

Hermes deliberately has no minute-scale learning steps, interval fuzzing, or
parameter optimizer. Product queue retries and caller-computed local-day floors
are represented separately from the raw mathematical schedule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hermes_vocab.models import (
    PARAMETER_FINGERPRINT,
    PARAMETERS_VERSION,
    SCHEDULER_KIND,
    SCHEDULER_VERSION,
    CardSchedule,
    CardScheduleState,
    ReviewRating,
)

DEFAULT_PARAMETERS = (
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
DESIRED_RETENTION = 0.90
MAXIMUM_INTERVAL_DAYS = 3650
_MINIMUM_STABILITY = 0.001
_MINIMUM_DIFFICULTY = 1.0
_MAXIMUM_DIFFICULTY = 10.0
_DECAY = -DEFAULT_PARAMETERS[20]
_FACTOR = 0.9 ** (1 / _DECAY) - 1
_RATING_VALUE = {
    ReviewRating.AGAIN: 1,
    ReviewRating.HARD: 2,
    ReviewRating.GOOD: 3,
    ReviewRating.EASY: 4,
}


@dataclass(frozen=True, slots=True)
class ScheduleTransition:
    before: CardSchedule
    after: CardSchedule
    rating: ReviewRating
    reviewed_at: datetime
    retrievability: float
    raw_due: datetime
    effective_due: datetime
    retry_same_session: bool


def _require_utc(value: datetime, name: str) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
        or value.tzname() != "UTC"
    ):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def retrievability(schedule: CardSchedule, at: datetime) -> float:
    """Return predicted recall probability at an explicit UTC instant."""

    _require_utc(at, "at")
    if schedule.last_review is None or schedule.stability is None:
        return 0.0
    elapsed_days = max(0, (at - schedule.last_review).days)
    return (1 + _FACTOR * elapsed_days / schedule.stability) ** _DECAY


def transition(
    schedule: CardSchedule,
    rating: ReviewRating,
    reviewed_at: datetime,
    *,
    same_session_retry: bool = False,
    due_floor_utc: datetime | None = None,
) -> ScheduleTransition:
    """Apply one rating without mutating the scalar schedule snapshot.

    ``same_session_retry`` identifies the one retry produced by a preceding
    Again. Its caller must provide the next configured local-day boundary as an
    already-converted UTC ``due_floor_utc``; timezone policy stays outside FSRS.
    """

    _require_utc(reviewed_at, "reviewed_at")
    if same_session_retry and rating is not ReviewRating.AGAIN:
        raise ValueError("same_session_retry is only valid for Again")
    if same_session_retry and due_floor_utc is None:
        raise ValueError("due_floor_utc is required for a retry Again")
    if due_floor_utc is not None:
        _require_utc(due_floor_utc, "due_floor_utc")

    rating_value = _RATING_VALUE[rating]
    current_retrievability = retrievability(schedule, reviewed_at)
    if schedule.state is CardScheduleState.NEW:
        stability = max(DEFAULT_PARAMETERS[rating_value - 1], _MINIMUM_STABILITY)
        difficulty = _initial_difficulty(rating_value)
    else:
        assert schedule.stability is not None
        assert schedule.difficulty is not None
        assert schedule.last_review is not None
        elapsed_days = (reviewed_at - schedule.last_review).days
        if elapsed_days < 1:
            stability = _short_term_stability(schedule.stability, rating_value)
        else:
            stability = _next_stability(
                schedule.difficulty,
                schedule.stability,
                current_retrievability,
                rating,
            )
        difficulty = _next_difficulty(schedule.difficulty, rating_value)

    next_state = (
        CardScheduleState.RELEARNING
        if rating is ReviewRating.AGAIN
        else CardScheduleState.REVIEW
    )
    raw_due = reviewed_at + timedelta(days=_next_interval(stability))
    after = CardSchedule(
        state=next_state,
        stability=stability,
        difficulty=difficulty,
        due=raw_due,
        last_review=reviewed_at,
        repetitions=schedule.repetitions + 1,
        lapses=schedule.lapses
        + int(rating is ReviewRating.AGAIN and schedule.state is not CardScheduleState.NEW),
        scheduler_kind=SCHEDULER_KIND,
        parameter_fingerprint=PARAMETER_FINGERPRINT,
        desired_retention=DESIRED_RETENTION,
        scheduler_version=SCHEDULER_VERSION,
        parameters_version=PARAMETERS_VERSION,
    )

    retry_same_session = rating is ReviewRating.AGAIN and not same_session_retry
    effective_due = raw_due
    if same_session_retry:
        assert due_floor_utc is not None
        effective_due = max(raw_due, due_floor_utc)

    return ScheduleTransition(
        before=schedule,
        after=after,
        rating=rating,
        reviewed_at=reviewed_at,
        retrievability=current_retrievability,
        raw_due=raw_due,
        effective_due=effective_due,
        retry_same_session=retry_same_session,
    )


def _clamp_difficulty(value: float) -> float:
    return min(max(value, _MINIMUM_DIFFICULTY), _MAXIMUM_DIFFICULTY)


def _initial_difficulty(rating_value: int) -> float:
    value = (
        DEFAULT_PARAMETERS[4]
        - math.exp(DEFAULT_PARAMETERS[5] * (rating_value - 1))
        + 1
    )
    return _clamp_difficulty(value)


def _next_difficulty(difficulty: float, rating_value: int) -> float:
    easy_difficulty = (
        DEFAULT_PARAMETERS[4]
        - math.exp(DEFAULT_PARAMETERS[5] * (4 - 1))
        + 1
    )
    delta = -DEFAULT_PARAMETERS[6] * (rating_value - 3)
    damped = difficulty + (10 - difficulty) * delta / 9
    reverted = (
        DEFAULT_PARAMETERS[7] * easy_difficulty
        + (1 - DEFAULT_PARAMETERS[7]) * damped
    )
    return _clamp_difficulty(reverted)


def _short_term_stability(stability: float, rating_value: int) -> float:
    multiplier = math.exp(
        DEFAULT_PARAMETERS[17]
        * (rating_value - 3 + DEFAULT_PARAMETERS[18])
    ) * stability ** -DEFAULT_PARAMETERS[19]
    if rating_value in (3, 4):
        multiplier = max(multiplier, 1.0)
    return max(stability * multiplier, _MINIMUM_STABILITY)


def _next_stability(
    difficulty: float,
    stability: float,
    current_retrievability: float,
    rating: ReviewRating,
) -> float:
    if rating is ReviewRating.AGAIN:
        long_term = (
            DEFAULT_PARAMETERS[11]
            * difficulty ** -DEFAULT_PARAMETERS[12]
            * ((stability + 1) ** DEFAULT_PARAMETERS[13] - 1)
            * math.exp((1 - current_retrievability) * DEFAULT_PARAMETERS[14])
        )
        short_term_limit = stability / math.exp(
            DEFAULT_PARAMETERS[17] * DEFAULT_PARAMETERS[18]
        )
        return max(min(long_term, short_term_limit), _MINIMUM_STABILITY)

    hard_penalty = DEFAULT_PARAMETERS[15] if rating is ReviewRating.HARD else 1.0
    easy_bonus = DEFAULT_PARAMETERS[16] if rating is ReviewRating.EASY else 1.0
    increase = (
        math.exp(DEFAULT_PARAMETERS[8])
        * (11 - difficulty)
        * stability ** -DEFAULT_PARAMETERS[9]
        * (math.exp((1 - current_retrievability) * DEFAULT_PARAMETERS[10]) - 1)
        * hard_penalty
        * easy_bonus
    )
    return max(stability * (1 + increase), _MINIMUM_STABILITY)


def _next_interval(stability: float) -> int:
    interval = (stability / _FACTOR) * (
        DESIRED_RETENTION ** (1 / _DECAY) - 1
    )
    return min(max(round(interval), 1), MAXIMUM_INTERVAL_DAYS)


__all__ = [
    "DEFAULT_PARAMETERS",
    "DESIRED_RETENTION",
    "MAXIMUM_INTERVAL_DAYS",
    "PARAMETER_FINGERPRINT",
    "PARAMETERS_VERSION",
    "SCHEDULER_KIND",
    "SCHEDULER_VERSION",
    "ScheduleTransition",
    "retrievability",
    "transition",
]
