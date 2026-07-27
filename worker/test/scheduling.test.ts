import { describe, expect, it } from "vitest";

import { CardScheduleState, ReviewRating } from "../src/domain/models";
import {
  DEFAULT_PARAMETERS,
  DESIRED_RETENTION,
  MAXIMUM_INTERVAL_DAYS,
  PARAMETERS_VERSION,
  PARAMETER_FINGERPRINT,
  SCHEDULER_KIND,
  SCHEDULER_VERSION,
  createCardSchedule,
  isReviewedSchedule,
  retrievability,
  transition,
} from "../src/domain/scheduling";
import type {
  CardSchedule,
  CardScheduleInit,
  ReviewedCardSchedule,
  ScheduleTransition,
} from "../src/domain/scheduling";

const NOW = new Date("2026-07-17T12:30:00.000Z");
const DAY_MS = 86_400_000;

function plusDays(instant: Date, days: number): Date {
  return new Date(instant.getTime() + days * DAY_MS);
}

function newCard(): CardSchedule {
  return createCardSchedule({ state: CardScheduleState.NEW, due: NOW });
}

/** Golden vectors are pinned to Python; agreement is required to 1e-9. */
function expectClose(actual: number, expected: number): void {
  expect(Math.abs(actual - expected)).toBeLessThanOrEqual(1e-9);
}

interface CardPayload {
  readonly state: string;
  readonly due: string;
  readonly stability: number | null;
  readonly difficulty: number | null;
  readonly last_review: string | null;
  readonly repetitions: number;
  readonly lapses: number;
  readonly scheduler_kind: string;
  readonly scheduler_version: string;
  readonly parameters_version: string;
  readonly parameter_fingerprint: string;
  readonly desired_retention: number;
}

interface TransitionPayload {
  readonly before: CardPayload;
  readonly after: CardPayload;
  readonly rating: string;
  readonly reviewed_at: string;
  readonly retrievability: number;
  readonly raw_due: string;
  readonly effective_due: string;
  readonly retry_same_session: boolean;
}

function cardPayload(card: CardSchedule): CardPayload {
  return {
    state: card.state,
    due: card.due.toISOString(),
    stability: card.stability,
    difficulty: card.difficulty,
    last_review: card.lastReview === null ? null : card.lastReview.toISOString(),
    repetitions: card.repetitions,
    lapses: card.lapses,
    scheduler_kind: card.schedulerKind,
    scheduler_version: card.schedulerVersion,
    parameters_version: card.parametersVersion,
    parameter_fingerprint: card.parameterFingerprint,
    desired_retention: card.desiredRetention,
  };
}

function parseState(value: string): CardScheduleState {
  const state = Object.values(CardScheduleState).find((candidate) => candidate === value);
  if (state === undefined) throw new Error(`unknown card schedule state: ${value}`);
  return state;
}

function parseRating(value: string): ReviewRating {
  const rating = Object.values(ReviewRating).find((candidate) => candidate === value);
  if (rating === undefined) throw new Error(`unknown review rating: ${value}`);
  return rating;
}

function cardFromPayload(payload: CardPayload): CardSchedule {
  return createCardSchedule({
    state: parseState(payload.state),
    due: new Date(payload.due),
    stability: payload.stability,
    difficulty: payload.difficulty,
    lastReview: payload.last_review === null ? null : new Date(payload.last_review),
    repetitions: payload.repetitions,
    lapses: payload.lapses,
    schedulerKind: payload.scheduler_kind,
    schedulerVersion: payload.scheduler_version,
    parametersVersion: payload.parameters_version,
    parameterFingerprint: payload.parameter_fingerprint,
    desiredRetention: payload.desired_retention,
  });
}

function reviewedFromPayload(payload: CardPayload): ReviewedCardSchedule {
  const card = cardFromPayload(payload);
  if (!isReviewedSchedule(card)) throw new Error("expected a reviewed schedule");
  return card;
}

describe("pinned py-fsrs 6.3.1 golden vectors", () => {
  const firstTransitions = [
    {
      rating: ReviewRating.AGAIN,
      stability: 0.212,
      difficulty: 6.4133,
      intervalDays: 1,
      state: CardScheduleState.RELEARNING,
    },
    {
      rating: ReviewRating.HARD,
      stability: 1.2931,
      difficulty: 5.112170705601055,
      intervalDays: 1,
      state: CardScheduleState.REVIEW,
    },
    {
      rating: ReviewRating.GOOD,
      stability: 2.3065,
      difficulty: 2.118103970459015,
      intervalDays: 2,
      state: CardScheduleState.REVIEW,
    },
    {
      rating: ReviewRating.EASY,
      stability: 8.2956,
      difficulty: 1.0,
      intervalDays: 8,
      state: CardScheduleState.REVIEW,
    },
  ] as const;

  it.each(firstTransitions)(
    "first $rating transition matches the pinned values",
    ({ rating, stability, difficulty, intervalDays, state }) => {
      const result = transition(newCard(), rating, NOW);

      expect(result.before).toEqual(newCard());
      expect(result.after.state).toBe(state);
      expectClose(result.after.stability, stability);
      expectClose(result.after.difficulty, difficulty);
      expect(result.after.due).toEqual(plusDays(NOW, intervalDays));
      expect(result.after.lastReview).toEqual(NOW);
      expect(result.after.repetitions).toBe(1);
      expect(result.after.lapses).toBe(0);
      expect(result.rawDue).toEqual(result.after.due);
    },
  );

  it("matches the later REVIEW-Good vector", () => {
    const card = transition(newCard(), ReviewRating.GOOD, NOW).after;

    const result = transition(card, ReviewRating.GOOD, plusDays(NOW, 20));

    expect(result.after.state).toBe(CardScheduleState.REVIEW);
    expectClose(result.after.stability, 32.78537806272411);
    expectClose(result.after.difficulty, 2.1112142357853942);
    expect(result.after.due).toEqual(new Date("2026-09-08T12:30:00.000Z"));
  });

  it("matches the later RELEARNING-Again vector", () => {
    const first = transition(newCard(), ReviewRating.AGAIN, NOW);

    const result = transition(first.after, ReviewRating.AGAIN, NOW, {
      sameSessionRetry: true,
      dueFloorUtc: plusDays(NOW, 2),
    });

    expect(result.after.state).toBe(CardScheduleState.RELEARNING);
    expectClose(result.after.stability, 0.08335671711031604);
    expectClose(result.after.difficulty, 8.806304468856837);
    expect(result.rawDue).toEqual(plusDays(NOW, 1));
  });
});

describe("retrievability", () => {
  it("is lower when overdue and lengthens the next interval", () => {
    const card = transition(newCard(), ReviewRating.GOOD, NOW).after;
    const onTime = card.due;
    const overdue = plusDays(NOW, 20);

    const onTimeRetrievability = retrievability(card, onTime);
    const overdueRetrievability = retrievability(card, overdue);
    const onTimeResult = transition(card, ReviewRating.GOOD, onTime);
    const overdueResult = transition(card, ReviewRating.GOOD, overdue);

    expect(overdueRetrievability).toBeLessThan(onTimeRetrievability);
    expectClose(overdueResult.retrievability, overdueRetrievability);
    expect(overdueResult.after.stability).toBeGreaterThan(onTimeResult.after.stability);
    expect(overdueResult.after.due.getTime()).toBeGreaterThan(onTimeResult.after.due.getTime());
  });

  it("is zero for a card that has never been reviewed", () => {
    expect(retrievability(newCard(), NOW)).toBe(0);
  });
});

describe("determinism", () => {
  it("treats a zero-elapsed early review as safe and repeatable", () => {
    const card = transition(newCard(), ReviewRating.GOOD, NOW).after;

    const first = transition(card, ReviewRating.GOOD, NOW);
    const second = transition(card, ReviewRating.GOOD, NOW);

    expect(first).toEqual(second);
    expectClose(first.retrievability, 1.0);
    expect(first.after.stability).toBeGreaterThanOrEqual(card.stability);
  });
});

describe("instant validation", () => {
  const invalidInstants = [
    { label: "unparseable text", instant: new Date("nonsense") },
    { label: "NaN epoch", instant: new Date(Number.NaN) },
    { label: "out-of-range epoch", instant: new Date(8.64e15 + 1) },
  ];

  it.each(invalidInstants)("rejects an $label instant without mutating the card", ({ instant }) => {
    const card = transition(newCard(), ReviewRating.GOOD, NOW).after;

    expect(() => transition(card, ReviewRating.GOOD, instant)).toThrow(/timezone-aware UTC/);
    expect(() => retrievability(card, instant)).toThrow(/timezone-aware UTC/);

    expect(card.lastReview).toEqual(NOW);
    expect(card.repetitions).toBe(1);
  });

  it("rejects an invalid due or lastReview instant", () => {
    expect(() =>
      createCardSchedule({ state: CardScheduleState.NEW, due: new Date(Number.NaN) }),
    ).toThrow(/due must be a timezone-aware UTC datetime/);
    expect(() =>
      createCardSchedule({
        state: CardScheduleState.REVIEW,
        stability: 2.0,
        difficulty: 5.0,
        due: NOW,
        lastReview: new Date(Number.NaN),
        repetitions: 1,
      }),
    ).toThrow(/lastReview must be a timezone-aware UTC datetime/);
  });
});

describe("card-state invariants", () => {
  it("requires lastReview on a reviewed schedule", () => {
    expect(() =>
      createCardSchedule({
        state: CardScheduleState.REVIEW,
        stability: 2.0,
        difficulty: 5.0,
        due: NOW,
        repetitions: 1,
      }),
    ).toThrow(/lastReview/);
  });

  it("requires at least one repetition on a reviewed schedule", () => {
    expect(() =>
      createCardSchedule({
        state: CardScheduleState.REVIEW,
        stability: 2.0,
        difficulty: 5.0,
        due: NOW,
        lastReview: NOW,
      }),
    ).toThrow(/at least one repetition/);
  });

  it("requires stability and difficulty on a reviewed schedule", () => {
    expect(() =>
      createCardSchedule({
        state: CardScheduleState.RELEARNING,
        due: NOW,
        lastReview: NOW,
        repetitions: 1,
      }),
    ).toThrow(/reviewed schedules require stability and difficulty/);
  });

  it.each([0.0, -1.0, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    "rejects non-positive or non-finite stability %p",
    (stability) => {
      expect(() =>
        createCardSchedule({
          state: CardScheduleState.REVIEW,
          stability,
          difficulty: 5.0,
          due: NOW,
          lastReview: NOW,
          repetitions: 1,
        }),
      ).toThrow(/stability/);
    },
  );

  it.each([0.0, 10.1, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    "rejects non-finite or out-of-range difficulty %p",
    (difficulty) => {
      expect(() =>
        createCardSchedule({
          state: CardScheduleState.REVIEW,
          stability: 2.0,
          difficulty,
          due: NOW,
          lastReview: NOW,
          repetitions: 1,
        }),
      ).toThrow(/difficulty/);
    },
  );

  const newCardOverrides: readonly { label: string; overrides: Partial<CardScheduleInit> }[] = [
    { label: "stability and difficulty", overrides: { stability: 2.0, difficulty: 5.0 } },
    { label: "lastReview", overrides: { lastReview: NOW } },
    { label: "repetitions", overrides: { repetitions: 1 } },
    { label: "lapses", overrides: { lapses: 1 } },
  ];

  it.each(newCardOverrides)("rejects $label on a new schedule", ({ overrides }) => {
    expect(() =>
      createCardSchedule({ state: CardScheduleState.NEW, due: NOW, ...overrides }),
    ).toThrow(/new schedules/);
  });

  it.each([
    { repetitions: -1, lapses: 0 },
    { repetitions: 0, lapses: -1 },
    { repetitions: 1, lapses: 2 },
  ])("rejects counters repetitions=$repetitions lapses=$lapses", ({ repetitions, lapses }) => {
    expect(() =>
      createCardSchedule({
        state: CardScheduleState.REVIEW,
        stability: 2.0,
        difficulty: 5.0,
        due: NOW,
        lastReview: NOW,
        repetitions,
        lapses,
      }),
    ).toThrow(/counters/);
  });
});

describe("immutability and scheduler metadata", () => {
  it("freezes snapshots and carries metadata through a transition", () => {
    const card = newCard();
    const mutableView: { repetitions: number } = card;

    expect(() => {
      mutableView.repetitions = 9;
    }).toThrow(TypeError);
    expect(card.repetitions).toBe(0);

    const result = transition(card, ReviewRating.HARD, NOW);

    expect(DEFAULT_PARAMETERS).toEqual([
      0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796,
      1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
    ]);
    expect(DEFAULT_PARAMETERS).toHaveLength(21);
    expect(DESIRED_RETENTION).toBe(0.9);
    expect(MAXIMUM_INTERVAL_DAYS).toBe(3650);
    expect(SCHEDULER_KIND).toBe("fsrs-6");
    expect(SCHEDULER_VERSION).toBe("fsrs-6.3.1-hermes-1");
    expect(PARAMETERS_VERSION).toBe("py-fsrs-6.3.1-default");
    expect(PARAMETER_FINGERPRINT).toBe(
      "sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56",
    );

    for (const snapshot of [result.before, result.after]) {
      expect(snapshot.schedulerKind).toBe(SCHEDULER_KIND);
      expect(snapshot.schedulerVersion).toBe(SCHEDULER_VERSION);
      expect(snapshot.parametersVersion).toBe(PARAMETERS_VERSION);
      expect(snapshot.parameterFingerprint).toBe(PARAMETER_FINGERPRINT);
      expect(snapshot.desiredRetention).toBe(DESIRED_RETENTION);
    }
  });
});

describe("same-session Again retries", () => {
  it("marks exactly one same-session retry on the first Again", () => {
    const result = transition(newCard(), ReviewRating.AGAIN, NOW);

    expect(result.retrySameSession).toBe(true);
    expect(result.effectiveDue).toEqual(result.rawDue);
    expect(result.after.state).toBe(CardScheduleState.RELEARNING);
  });

  it("keeps the raw transition and applies the due floor on the second Again", () => {
    const first = transition(newCard(), ReviewRating.AGAIN, NOW);
    const dueFloor = plusDays(NOW, 2);

    const second = transition(first.after, ReviewRating.AGAIN, NOW, {
      sameSessionRetry: true,
      dueFloorUtc: dueFloor,
    });

    expect(second.retrySameSession).toBe(false);
    expect(second.after.state).toBe(CardScheduleState.RELEARNING);
    expect(second.after.due).toEqual(second.rawDue);
    expect(second.rawDue.getTime()).toBeLessThan(dueFloor.getTime());
    expect(second.effectiveDue).toEqual(dueFloor);
    expect(second.after.repetitions).toBe(2);
    expect(second.after.lapses).toBe(1);
  });

  it("requires an explicit UTC floor for the retry Again", () => {
    const first = transition(newCard(), ReviewRating.AGAIN, NOW);

    expect(() =>
      transition(first.after, ReviewRating.AGAIN, NOW, { sameSessionRetry: true }),
    ).toThrow(/dueFloorUtc/);
    expect(() =>
      transition(first.after, ReviewRating.AGAIN, NOW, {
        sameSessionRetry: true,
        dueFloorUtc: new Date("not a date"),
      }),
    ).toThrow(/timezone-aware UTC/);
  });

  it("rejects a same-session retry for any rating other than Again", () => {
    const first = transition(newCard(), ReviewRating.AGAIN, NOW);

    expect(() =>
      transition(first.after, ReviewRating.GOOD, NOW, {
        sameSessionRetry: true,
        dueFloorUtc: plusDays(NOW, 2),
      }),
    ).toThrow(/sameSessionRetry is only valid for Again/);
  });
});

describe("JSON round trip", () => {
  it("preserves the scalar schedule and transition metadata", () => {
    const first = transition(newCard(), ReviewRating.AGAIN, NOW);
    const original = transition(first.after, ReviewRating.AGAIN, NOW, {
      sameSessionRetry: true,
      dueFloorUtc: plusDays(NOW, 2),
    });

    const serialized = JSON.stringify({
      before: cardPayload(original.before),
      after: cardPayload(original.after),
      rating: original.rating,
      reviewed_at: original.reviewedAt.toISOString(),
      retrievability: original.retrievability,
      raw_due: original.rawDue.toISOString(),
      effective_due: original.effectiveDue.toISOString(),
      retry_same_session: original.retrySameSession,
    });

    const payload: TransitionPayload = JSON.parse(serialized);
    const restored: ScheduleTransition = {
      before: cardFromPayload(payload.before),
      after: reviewedFromPayload(payload.after),
      rating: parseRating(payload.rating),
      reviewedAt: new Date(payload.reviewed_at),
      retrievability: payload.retrievability,
      rawDue: new Date(payload.raw_due),
      effectiveDue: new Date(payload.effective_due),
      retrySameSession: payload.retry_same_session,
    };

    expect(restored).toEqual(original);
    expect(restored.before.schedulerKind).toBe(SCHEDULER_KIND);
    expect(restored.after.schedulerVersion).toBe(SCHEDULER_VERSION);
    expect(restored.after.parameterFingerprint).toBe(PARAMETER_FINGERPRINT);
    expect(restored.after.desiredRetention).toBe(DESIRED_RETENTION);
  });
});
