/**
 * Deterministic, dependency-free FSRS-6 scheduling.
 *
 * A direct port of `src/hermes_vocab/scheduling.py`, which is the source of
 * truth. The equations and default weights are independently expressed from the
 * published FSRS-6 memory model and py-fsrs 6.3.1. py-fsrs is Copyright (c)
 * Jarrett Ye and contributors and is available under the MIT License:
 * https://github.com/open-spaced-repetition/py-fsrs/tree/v6.3.1
 *
 * Hermes deliberately has no minute-scale learning steps, interval fuzzing, or
 * parameter optimizer. Product queue retries and caller-computed local-day
 * floors are represented separately from the raw mathematical schedule.
 */

import { CardScheduleState, ReviewRating } from "./models";

export const DEFAULT_PARAMETERS: readonly number[] = Object.freeze([
  0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835,
  0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
]);

export const DESIRED_RETENTION = 0.9;
export const MAXIMUM_INTERVAL_DAYS = 3650;

export const SCHEDULER_KIND = "fsrs-6";
export const SCHEDULER_VERSION = "fsrs-6.3.1-hermes-1";
export const PARAMETERS_VERSION = "py-fsrs-6.3.1-default";
export const PARAMETER_FINGERPRINT =
  "sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56";

const MINIMUM_STABILITY = 0.001;
const MINIMUM_DIFFICULTY = 1.0;
const MAXIMUM_DIFFICULTY = 10.0;
const DECAY = -DEFAULT_PARAMETERS[20];
const FACTOR = 0.9 ** (1 / DECAY) - 1;
const DAY_MS = 86_400_000;

const RATING_VALUE: Readonly<Record<ReviewRating, number>> = Object.freeze({
  [ReviewRating.AGAIN]: 1,
  [ReviewRating.HARD]: 2,
  [ReviewRating.GOOD]: 3,
  [ReviewRating.EASY]: 4,
});

/** Immutable scalar snapshot of one card's FSRS state. */
export interface CardSchedule {
  readonly state: CardScheduleState;
  readonly due: Date;
  readonly stability: number | null;
  readonly difficulty: number | null;
  readonly lastReview: Date | null;
  readonly repetitions: number;
  readonly lapses: number;
  readonly schedulerKind: string;
  readonly parameterFingerprint: string;
  readonly desiredRetention: number;
  readonly schedulerVersion: string;
  readonly parametersVersion: string;
}

/** A schedule that has been through at least one review. */
export interface ReviewedCardSchedule extends CardSchedule {
  readonly state: typeof CardScheduleState.REVIEW | typeof CardScheduleState.RELEARNING;
  readonly stability: number;
  readonly difficulty: number;
  readonly lastReview: Date;
}

export interface CardScheduleInit {
  readonly state: CardScheduleState;
  readonly due: Date;
  readonly stability?: number | null;
  readonly difficulty?: number | null;
  readonly lastReview?: Date | null;
  readonly repetitions?: number;
  readonly lapses?: number;
  readonly schedulerKind?: string;
  readonly parameterFingerprint?: string;
  readonly desiredRetention?: number;
  readonly schedulerVersion?: string;
  readonly parametersVersion?: string;
}

export interface ScheduleTransition {
  readonly before: CardSchedule;
  readonly after: ReviewedCardSchedule;
  readonly rating: ReviewRating;
  readonly reviewedAt: Date;
  readonly retrievability: number;
  readonly rawDue: Date;
  readonly effectiveDue: Date;
  readonly retrySameSession: boolean;
}

export interface TransitionOptions {
  /** Marks an Again submitted for a retry occurrence in the current session. */
  readonly sameSessionRetry?: boolean;
  /** Whether every Again may append a retry to the current session. */
  readonly allowSameSessionRetry?: boolean;
  /** Next configured local-day boundary, already converted to UTC by the caller. */
  readonly dueFloorUtc?: Date | null;
}

/**
 * A `Date` is always a UTC instant, so the Python timezone check reduces to
 * rejecting the invalid (NaN) instant. The message is kept identical so callers
 * and tests read the same contract in both languages.
 */
function requireUtc(value: Date, name: string): void {
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) {
    throw new RangeError(`${name} must be a timezone-aware UTC datetime`);
  }
}

/** Whole days between two instants, floored like Python's `timedelta.days`. */
function elapsedDays(from: Date, to: Date): number {
  return Math.floor((to.getTime() - from.getTime()) / DAY_MS);
}

export function isReviewedSchedule(schedule: CardSchedule): schedule is ReviewedCardSchedule {
  return (
    schedule.state !== CardScheduleState.NEW &&
    schedule.stability !== null &&
    schedule.difficulty !== null &&
    schedule.lastReview !== null
  );
}

export function createCardSchedule(init: CardScheduleInit): CardSchedule {
  const schedule: CardSchedule = {
    state: init.state,
    due: init.due,
    stability: init.stability ?? null,
    difficulty: init.difficulty ?? null,
    lastReview: init.lastReview ?? null,
    repetitions: init.repetitions ?? 0,
    lapses: init.lapses ?? 0,
    schedulerKind: init.schedulerKind ?? SCHEDULER_KIND,
    parameterFingerprint: init.parameterFingerprint ?? PARAMETER_FINGERPRINT,
    desiredRetention: init.desiredRetention ?? DESIRED_RETENTION,
    schedulerVersion: init.schedulerVersion ?? SCHEDULER_VERSION,
    parametersVersion: init.parametersVersion ?? PARAMETERS_VERSION,
  };

  requireUtc(schedule.due, "due");
  if (schedule.lastReview !== null) requireUtc(schedule.lastReview, "lastReview");
  if (schedule.repetitions < 0 || schedule.lapses < 0) {
    throw new RangeError("review counters cannot be negative");
  }
  if (schedule.state === CardScheduleState.NEW) {
    if (
      schedule.stability !== null ||
      schedule.difficulty !== null ||
      schedule.lastReview !== null ||
      schedule.repetitions !== 0 ||
      schedule.lapses !== 0
    ) {
      throw new RangeError("new schedules cannot contain review state");
    }
    return Object.freeze(schedule);
  }
  if (schedule.lapses > schedule.repetitions) {
    throw new RangeError("review counters cannot have more lapses than repetitions");
  }
  if (schedule.stability === null || schedule.difficulty === null) {
    throw new RangeError("reviewed schedules require stability and difficulty");
  }
  if (schedule.lastReview === null) throw new RangeError("reviewed schedules require lastReview");
  if (schedule.repetitions < 1) {
    throw new RangeError("reviewed schedules require at least one repetition");
  }
  if (!Number.isFinite(schedule.stability) || schedule.stability <= 0) {
    throw new RangeError("stability must be finite and positive");
  }
  if (
    !Number.isFinite(schedule.difficulty) ||
    schedule.difficulty < MINIMUM_DIFFICULTY ||
    schedule.difficulty > MAXIMUM_DIFFICULTY
  ) {
    throw new RangeError("difficulty must be finite and between 1 and 10");
  }
  return Object.freeze(schedule);
}

/** Predicted recall probability at an explicit UTC instant. */
export function retrievability(schedule: CardSchedule, at: Date): number {
  requireUtc(at, "at");
  if (schedule.lastReview === null || schedule.stability === null) return 0;
  const days = Math.max(0, elapsedDays(schedule.lastReview, at));
  return (1 + (FACTOR * days) / schedule.stability) ** DECAY;
}

/**
 * Apply one rating without mutating the scalar schedule snapshot.
 *
 * `sameSessionRetry` identifies an Again submitted for a retry occurrence.
 * Its caller must provide the next configured local-day boundary as an
 * already-converted UTC `dueFloorUtc`; timezone policy stays outside FSRS.
 */
export function transition(
  schedule: CardSchedule,
  rating: ReviewRating,
  reviewedAt: Date,
  options: TransitionOptions = {},
): ScheduleTransition {
  const sameSessionRetry = options.sameSessionRetry ?? false;
  const allowSameSessionRetry = options.allowSameSessionRetry ?? true;
  const dueFloorUtc = options.dueFloorUtc ?? null;

  requireUtc(reviewedAt, "reviewedAt");
  if (sameSessionRetry && rating !== ReviewRating.AGAIN) {
    throw new RangeError("sameSessionRetry is only valid for Again");
  }
  if (sameSessionRetry && dueFloorUtc === null) {
    throw new RangeError("dueFloorUtc is required for a retry Again");
  }
  if (dueFloorUtc !== null) requireUtc(dueFloorUtc, "dueFloorUtc");

  const ratingValue = RATING_VALUE[rating];
  const currentRetrievability = retrievability(schedule, reviewedAt);

  let stability: number;
  let difficulty: number;
  if (schedule.state === CardScheduleState.NEW) {
    stability = Math.max(DEFAULT_PARAMETERS[ratingValue - 1], MINIMUM_STABILITY);
    difficulty = initialDifficulty(ratingValue);
  } else {
    if (!isReviewedSchedule(schedule)) {
      throw new RangeError("reviewed schedules require stability and difficulty");
    }
    stability =
      elapsedDays(schedule.lastReview, reviewedAt) < 1
        ? shortTermStability(schedule.stability, ratingValue)
        : nextStability(schedule.difficulty, schedule.stability, currentRetrievability, rating);
    difficulty = nextDifficulty(schedule.difficulty, ratingValue);
  }

  const rawDue = new Date(reviewedAt.getTime() + nextInterval(stability) * DAY_MS);
  const isLapse = rating === ReviewRating.AGAIN && schedule.state !== CardScheduleState.NEW;
  const after = createCardSchedule({
    state: rating === ReviewRating.AGAIN ? CardScheduleState.RELEARNING : CardScheduleState.REVIEW,
    stability,
    difficulty,
    due: rawDue,
    lastReview: reviewedAt,
    repetitions: schedule.repetitions + 1,
    lapses: schedule.lapses + (isLapse ? 1 : 0),
    schedulerKind: SCHEDULER_KIND,
    parameterFingerprint: PARAMETER_FINGERPRINT,
    desiredRetention: DESIRED_RETENTION,
    schedulerVersion: SCHEDULER_VERSION,
    parametersVersion: PARAMETERS_VERSION,
  });
  if (!isReviewedSchedule(after)) throw new Error("unreachable: transitions produce reviewed cards");

  const effectiveDue =
    sameSessionRetry && dueFloorUtc !== null && dueFloorUtc.getTime() > rawDue.getTime()
      ? dueFloorUtc
      : rawDue;

  return Object.freeze({
    before: schedule,
    after,
    rating,
    reviewedAt,
    retrievability: currentRetrievability,
    rawDue,
    effectiveDue,
    retrySameSession: allowSameSessionRetry && rating === ReviewRating.AGAIN,
  });
}

function clampDifficulty(value: number): number {
  return Math.min(Math.max(value, MINIMUM_DIFFICULTY), MAXIMUM_DIFFICULTY);
}

function initialDifficulty(ratingValue: number): number {
  return clampDifficulty(
    DEFAULT_PARAMETERS[4] - Math.exp(DEFAULT_PARAMETERS[5] * (ratingValue - 1)) + 1,
  );
}

function nextDifficulty(difficulty: number, ratingValue: number): number {
  const easyDifficulty = DEFAULT_PARAMETERS[4] - Math.exp(DEFAULT_PARAMETERS[5] * (4 - 1)) + 1;
  const delta = -DEFAULT_PARAMETERS[6] * (ratingValue - 3);
  const damped = difficulty + ((10 - difficulty) * delta) / 9;
  return clampDifficulty(
    DEFAULT_PARAMETERS[7] * easyDifficulty + (1 - DEFAULT_PARAMETERS[7]) * damped,
  );
}

function shortTermStability(stability: number, ratingValue: number): number {
  const raw =
    Math.exp(DEFAULT_PARAMETERS[17] * (ratingValue - 3 + DEFAULT_PARAMETERS[18])) *
    stability ** -DEFAULT_PARAMETERS[19];
  const multiplier = ratingValue === 3 || ratingValue === 4 ? Math.max(raw, 1.0) : raw;
  return Math.max(stability * multiplier, MINIMUM_STABILITY);
}

function nextStability(
  difficulty: number,
  stability: number,
  currentRetrievability: number,
  rating: ReviewRating,
): number {
  if (rating === ReviewRating.AGAIN) {
    const longTerm =
      DEFAULT_PARAMETERS[11] *
      difficulty ** -DEFAULT_PARAMETERS[12] *
      ((stability + 1) ** DEFAULT_PARAMETERS[13] - 1) *
      Math.exp((1 - currentRetrievability) * DEFAULT_PARAMETERS[14]);
    const shortTermLimit =
      stability / Math.exp(DEFAULT_PARAMETERS[17] * DEFAULT_PARAMETERS[18]);
    return Math.max(Math.min(longTerm, shortTermLimit), MINIMUM_STABILITY);
  }

  const hardPenalty = rating === ReviewRating.HARD ? DEFAULT_PARAMETERS[15] : 1.0;
  const easyBonus = rating === ReviewRating.EASY ? DEFAULT_PARAMETERS[16] : 1.0;
  const increase =
    Math.exp(DEFAULT_PARAMETERS[8]) *
    (11 - difficulty) *
    stability ** -DEFAULT_PARAMETERS[9] *
    (Math.exp((1 - currentRetrievability) * DEFAULT_PARAMETERS[10]) - 1) *
    hardPenalty *
    easyBonus;
  return Math.max(stability * (1 + increase), MINIMUM_STABILITY);
}

function nextInterval(stability: number): number {
  const interval = (stability / FACTOR) * (DESIRED_RETENTION ** (1 / DECAY) - 1);
  return Math.min(Math.max(roundHalfToEven(interval), 1), MAXIMUM_INTERVAL_DAYS);
}

/** Python's `round()` is banker's rounding; `Math.round()` is not. */
function roundHalfToEven(value: number): number {
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (fraction > 0.5) return floor + 1;
  if (fraction < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}
