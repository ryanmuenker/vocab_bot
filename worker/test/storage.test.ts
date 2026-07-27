import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import {
  CaptureStatus,
  CardDirection,
  EvaluationGrade,
  FinalizeStatus,
  ReviewRating,
  StudyMode,
  StudyMutationStatus,
  StudyPromptStatus,
  StudyQueueStatus,
  StudyStartStatus,
} from "../src/domain/models";
import type { Evaluation, FinalizeResult, SenseCard, StudyPromptSnapshot } from "../src/domain/models";
import { VocabularyStore } from "../src/storage/vocabulary-store";

const CARD: SenseCard = {
  partOfSpeech: "noun",
  definition: "a definition",
  exampleSentence: "An example sentence.",
};
const SECOND_CARD: SenseCard = {
  partOfSpeech: "verb",
  definition: "to define",
  exampleSentence: "I define it.",
};
const CORRECT: Evaluation = { grade: EvaluationGrade.CORRECT, feedback: "Accurate." };

function stub() {
  return env.VOCABULARY.getByName(`storage-${crypto.randomUUID()}`);
}

function rows<T extends Record<string, SqlStorageValue>>(
  storage: DurableObjectStorage,
  query: string,
  ...bindings: SqlStorageValue[]
): T[] {
  return Array.from(storage.sql.exec<T>(query, ...bindings));
}

/** Capture `count` single-sense entries one day apart, oldest first. */
function seedEntries(store: VocabularyStore, count: number, prefix = "entry"): void {
  for (let index = 1; index <= count; index += 1) {
    store.captureEntry(
      `${prefix}-${index}`,
      [CARD],
      new Date(`2026-06-${String(index).padStart(2, "0")}T00:00:00Z`),
    );
  }
}

/** Force a card into a reviewed state with an explicit effective due instant. */
function makeDue(storage: DurableObjectStorage, cardId: number, effectiveDue: string): void {
  Array.from(
    storage.sql.exec(
      `UPDATE vocabulary_cards
       SET state = 'review', stability = 4.0, difficulty = 5.0, due_at = ?,
           effective_due_at = ?, last_review_at = ?, repetitions = 1, lapses = 0,
           introduced_local_date = substr(?, 1, 10)
       WHERE id = ?`,
      effectiveDue,
      effectiveDue,
      "2026-06-01T00:00:00Z",
      effectiveDue,
      cardId,
    ),
  );
}

/** Prepare, deliver, answer, and finalize the current prompt in one step. */
function answerCurrent(
  store: VocabularyStore,
  rating: ReviewRating,
  now: Date,
  grade = CORRECT,
): { prompt: StudyPromptSnapshot; result: FinalizeResult } {
  const plan = store.currentPromptPlan(now);
  if (plan === null) throw new Error("no current prompt plan");
  const prompt =
    plan.snapshot.currentPrompt ??
    store.prepareCurrentPrompt(plan.promptKey, `Prompt for ${plan.promptKey}`, now);
  if (prompt === null) throw new Error("prompt not prepared");
  store.recordDelivery(prompt.id, {
    deliveryId: `msg-${prompt.id}`,
    contentFingerprint: `sha-${prompt.id}`,
    now,
  });
  store.recordAnswer(prompt.id, "an answer", grade, now);
  return { prompt, result: store.finalize(prompt.id, rating, now) };
}

describe("VocabularyStore capture", () => {
  it("projects one forward card per entry and one reverse card per sense atomically", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      const result = store.captureEntry(
        "  Straße  ",
        [CARD, SECOND_CARD],
        new Date("2026-07-20T00:00:00Z"),
      );

      expect(result.status).toBe(CaptureStatus.SAVED);
      expect(result.entry?.normalizedText).toBe("strasse");
      const cards = rows<{
        direction: string;
        sense_id: number | null;
        state: string;
        due_at: string;
        effective_due_at: string;
        scheduler_kind: string;
        created_at: string;
      }>(
        state.storage,
        "SELECT * FROM vocabulary_cards WHERE entry_id = ? ORDER BY id",
        result.entry!.id,
      );
      expect(cards.map(({ direction, sense_id }) => [direction, sense_id])).toEqual([
        ["forward", null],
        ["reverse", result.entry!.senses[0]!.id],
        ["reverse", result.entry!.senses[1]!.id],
      ]);
      for (const card of cards) {
        expect(card.state).toBe("new");
        expect(card.due_at).toBe("2026-07-20T00:00:00Z");
        expect(card.effective_due_at).toBe("2026-07-20T00:00:00Z");
        expect(card.created_at).toBe("2026-07-20T00:00:00Z");
        expect(card.scheduler_kind).toBe("fsrs-6");
      }
    });
  });

  it("leaves no cards behind when the capture aggregate is rejected", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      expect(store.captureEntry("invalid", [CARD, { ...CARD, partOfSpeech: " Noun " }]).status)
        .toBe(CaptureStatus.INVALID);
      store.captureEntry("kept", [CARD], new Date("2026-07-20T00:00:00Z"));
      expect(store.captureEntry("KEPT", [CARD]).status).toBe(CaptureStatus.ALREADY_EXISTS);

      const cards = rows<{ count: number }>(
        state.storage,
        "SELECT COUNT(*) AS count FROM vocabulary_cards",
      )[0]!.count;
      expect(cards).toBe(2);
    });
  });
});

describe("VocabularyStore selection", () => {
  it("orders due cards ahead of unseen ones and skips cards buried for the local day", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 3);
      // entry-1 forward is due later than entry-3 forward; entry-2 forward is buried.
      makeDue(state.storage, 1, "2026-07-20T06:00:00Z");
      makeDue(state.storage, 3, "2026-07-20T09:00:00Z");
      makeDue(state.storage, 5, "2026-07-20T03:00:00Z");
      Array.from(
        state.storage.sql.exec(
          "UPDATE vocabulary_cards SET buried_until_local_date = '2026-07-20' WHERE id = 3",
        ),
      );

      const started = store.startReview(new Date("2026-07-20T10:00:00Z"));
      expect(started.status).toBe(StudyStartStatus.STARTED);
      const cardIds = started.snapshot!.queue.map((item) => item.card.id);
      expect(cardIds.slice(0, 2)).toEqual([5, 1]);
      expect(cardIds).not.toContain(3);
    });
  });

  it("introduces at most five unseen cards per local day across sessions", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 6);

      const started = store.startReview(new Date("2026-07-20T10:00:00Z"));
      expect(started.snapshot!.queue).toHaveLength(5);
      expect(store.exitStudy(new Date("2026-07-20T10:05:00Z"))).toBe(StudyMutationStatus.COMPLETED);

      // The quota is per local day, so a second same-day session introduces none
      // beyond the five already carried over.
      const second = store.startReview(new Date("2026-07-20T11:00:00Z"));
      expect(second.snapshot!.queue).toHaveLength(5);
      expect(second.snapshot!.queue.map((item) => item.card.id)).toEqual([1, 2, 3, 4, 5]);
    });
  });

  it("reports an empty library rather than an empty session", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      const started = store.startReview(new Date("2026-07-20T10:00:00Z"));
      expect(started.status).toBe(StudyStartStatus.EMPTY);
      expect(rows(state.storage, "SELECT id FROM study_sessions")).toHaveLength(0);
    });
  });
});

describe("VocabularyStore prompt delivery gating", () => {
  it("keeps a prepared prompt unanswerable until a delivery is recorded", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 2);
      store.startReview(new Date("2026-07-20T10:00:00Z"));
      const plan = store.currentPromptPlan(new Date("2026-07-20T10:00:00Z"))!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "What does it mean?", new Date("2026-07-20T10:00:00Z"));

      expect(prompt!.status).toBe(StudyPromptStatus.PREPARED);
      expect(store.answerablePrompt()).toBeNull();
      expect(store.recordAnswer(prompt!.id, "guess", CORRECT)).toBeNull();

      const delivered = store.recordDelivery(prompt!.id, {
        deliveryId: "5150",
        contentFingerprint: "a".repeat(64),
      });
      expect(delivered!.status).toBe(StudyPromptStatus.DELIVERED);
      expect(store.answerablePrompt()?.id).toBe(prompt!.id);
      const attempt = rows<{ status: string; outbound_delivery_id: string; receipt_at: string }>(
        state.storage,
        "SELECT * FROM prompt_delivery_attempts WHERE prompt_id = ?",
        prompt!.id,
      );
      expect(attempt).toHaveLength(1);
      expect(attempt[0]).toMatchObject({ status: "delivered", outbound_delivery_id: "5150" });
    });
  });

  it("records a failed attempt without ever making the prompt answerable", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 2);
      store.startReview(new Date("2026-07-20T10:00:00Z"));
      const plan = store.currentPromptPlan(new Date("2026-07-20T10:00:00Z"))!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "What does it mean?", new Date("2026-07-20T10:00:00Z"))!;

      expect(store.recordDeliveryFailure(prompt.id, { error: "telegram_delivery_failed" })!.status)
        .toBe(StudyPromptStatus.PREPARED);
      expect(store.answerablePrompt()).toBeNull();
      expect(rows(state.storage, "SELECT id FROM prompt_delivery_attempts WHERE status = 'failed'"))
        .toHaveLength(1);

      // The same prompt stays retryable and can still be promoted later.
      expect(
        store.recordDelivery(prompt.id, { deliveryId: "7", contentFingerprint: "b".repeat(64) })!
          .status,
      ).toBe(StudyPromptStatus.DELIVERED);
      expect(
        rows<{ attempt_number: number }>(
          state.storage,
          "SELECT attempt_number FROM prompt_delivery_attempts WHERE prompt_id = ? ORDER BY id",
          prompt.id,
        ).map(({ attempt_number }) => attempt_number),
      ).toEqual([1, 2]);
    });
  });

  it("moves a delivered prompt to awaiting rating on an immutable answer draft", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 2);
      store.startReview(new Date("2026-07-20T10:00:00Z"));
      const plan = store.currentPromptPlan(new Date("2026-07-20T10:00:00Z"))!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "Question?", new Date("2026-07-20T10:00:00Z"))!;
      store.recordDelivery(prompt.id, { deliveryId: "9", contentFingerprint: "c".repeat(64) });

      expect(store.recordAnswer(prompt.id, "  ", CORRECT)).toBeNull();
      expect(store.recordAnswer(prompt.id, "answer", { grade: EvaluationGrade.CORRECT, feedback: " " }))
        .toBeNull();
      expect(store.recordAnswer(prompt.id, "answer", CORRECT)!.status)
        .toBe(StudyPromptStatus.ANSWERED);
      expect(store.awaitingRating()?.id).toBe(prompt.id);
      expect(store.currentAnswerContext()?.draft).toMatchObject({
        submittedAnswer: "answer",
        evaluation: CORRECT,
      });
      expect(() =>
        Array.from(
          state.storage.sql.exec("UPDATE answer_drafts SET submitted_answer = 'x' WHERE prompt_id = ?", prompt.id),
        ),
      ).toThrow(/immutable/u);
    });
  });
});

describe("VocabularyStore finalization", () => {
  it("applies the FSRS transition, buries siblings, and advances the queue", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      store.captureEntry("obdurate", [CARD], new Date("2026-06-01T00:00:00Z"));
      store.captureEntry("laconic", [CARD], new Date("2026-06-02T00:00:00Z"));
      const now = new Date("2026-07-20T10:00:00Z");
      store.startReview(now);

      const { prompt, result } = answerCurrent(store, ReviewRating.GOOD, now);
      expect(result.status).toBe(FinalizeStatus.COMPLETED);
      expect(result.transition!.after.repetitions).toBe(1);

      const card = rows<{
        state: string;
        stability: number;
        difficulty: number;
        effective_due_at: string;
        repetitions: number;
      }>(state.storage, "SELECT * FROM vocabulary_cards WHERE id = 1")[0]!;
      expect(card.state).toBe("review");
      expect(card.repetitions).toBe(1);
      expect(card.stability).toBeCloseTo(result.transition!.after.stability!, 12);
      expect(new Date(card.effective_due_at).getTime())
        .toBe(result.transition!.effectiveDue.getTime());

      // The reverse sibling of the same entry is buried for the local day and
      // dropped from the queue without moving its due time.
      const sibling = rows<{ buried_until_local_date: string | null; effective_due_at: string }>(
        state.storage,
        "SELECT * FROM vocabulary_cards WHERE id = 2",
      )[0]!;
      expect(sibling.buried_until_local_date).toBe("2026-07-20");
      expect(sibling.effective_due_at).toBe("2026-06-01T00:00:00Z");
      const queue = result.snapshot!.queue;
      expect(queue.find((item) => item.card.id === 2)!.status).toBe(StudyQueueStatus.SKIPPED);
      expect(queue.find((item) => item.card.id === 3)!.status).toBe(StudyQueueStatus.CURRENT);
      expect(
        rows<{ status: string }>(state.storage, "SELECT status FROM study_prompts WHERE id = ?", prompt.id)[0],
      ).toEqual({ status: "completed" });
      expect(
        rows<{ count: number }>(state.storage, "SELECT COUNT(*) AS count FROM review_attempts")[0]!.count,
      ).toBe(1);
    });
  });

  it("refuses to finalize a prompt that has no answer and one already completed", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 2);
      const now = new Date("2026-07-20T10:00:00Z");
      store.startReview(now);
      const plan = store.currentPromptPlan(now)!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "Question?", now)!;
      store.recordDelivery(prompt.id, { deliveryId: "1", contentFingerprint: "d".repeat(64) });

      expect(store.finalize(prompt.id, ReviewRating.GOOD, now).status).toBe(FinalizeStatus.NO_ANSWER);
      store.recordAnswer(prompt.id, "answer", CORRECT, now);
      expect(store.finalize(prompt.id, ReviewRating.GOOD, now).status).toBe(FinalizeStatus.COMPLETED);
      expect(store.finalize(prompt.id, ReviewRating.GOOD, now).status).toBe(FinalizeStatus.STALE);
    });
  });

  it("appends exactly one tail retry and pushes it past the current local day", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      store.captureEntry("obdurate", [CARD], new Date("2026-06-01T00:00:00Z"));
      const now = new Date("2026-07-20T10:00:00Z");
      store.startReview(now);

      const first = answerCurrent(store, ReviewRating.AGAIN, now);
      expect(first.result.transition!.retrySameSession).toBe(true);
      const retryItems = first.result.snapshot!.queue.filter(
        (item) => item.retryOfQueueItemId !== null,
      );
      expect(retryItems).toHaveLength(1);
      expect(retryItems[0]!.status).toBe(StudyQueueStatus.CURRENT);

      const second = answerCurrent(store, ReviewRating.AGAIN, now);
      expect(second.result.transition!.retrySameSession).toBe(false);
      expect(second.result.snapshot!.queue.filter((item) => item.retryOfQueueItemId !== null))
        .toHaveLength(1);
      // The floor is `dueFloorUtc`; FSRS never schedules inside 24 hours, so the
      // raw due already clears the next local midnight and no clamp is applied.
      expect(second.result.transition!.effectiveDue.getTime())
        .toBeGreaterThanOrEqual(Date.parse("2026-07-21T00:00:00Z"));
      expect(second.result.transition!.effectiveDue.getTime())
        .toBe(second.result.transition!.rawDue.getTime());
      expect(second.result.snapshot!.status).toBe("completed");
      expect(
        rows<{ is_same_session_retry: number }>(
          state.storage,
          "SELECT is_same_session_retry FROM review_attempts ORDER BY id",
        ).map(({ is_same_session_retry }) => is_same_session_retry),
      ).toEqual([0, 1]);
    });
  });
});

describe("VocabularyStore directional tests", () => {
  it("requires five cards from five distinct entries and starts nothing on a shortfall", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 4);
      const short = store.startTest(CardDirection.FORWARD, new Date("2026-07-20T10:00:00Z"));
      expect(short.status).toBe(StudyStartStatus.EMPTY);
      expect(short.availableCount).toBe(4);
      expect(rows(state.storage, "SELECT id FROM study_sessions")).toHaveLength(0);
      expect(rows(state.storage, "SELECT id FROM study_queue")).toHaveLength(0);

      store.captureEntry("entry-5", [CARD, SECOND_CARD], new Date("2026-06-05T00:00:00Z"));
      const started = store.startTest(CardDirection.FORWARD, new Date("2026-07-20T10:00:00Z"));
      expect(started.status).toBe(StudyStartStatus.STARTED);
      expect(started.snapshot!.mode).toBe(StudyMode.TEST_FORWARD);
      const entryIds = started.snapshot!.queue.map((item) => item.card.entryId);
      expect(entryIds).toHaveLength(5);
      expect(new Set(entryIds).size).toBe(5);
      expect(started.snapshot!.queue.every((item) => item.card.direction === "forward")).toBe(true);
    });
  });

  it("selects one reverse card per entry and refuses a conflicting review", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      for (let index = 1; index <= 5; index += 1) {
        store.captureEntry(
          `multi-${index}`,
          [CARD, SECOND_CARD],
          new Date(`2026-06-0${index}T00:00:00Z`),
        );
      }
      const started = store.startTest(CardDirection.REVERSE, new Date("2026-07-20T10:00:00Z"));
      expect(started.status).toBe(StudyStartStatus.STARTED);
      expect(new Set(started.snapshot!.queue.map((item) => item.card.entryId)).size).toBe(5);
      expect(started.snapshot!.queue.every((item) => item.card.senseId !== null)).toBe(true);

      expect(store.startReview(new Date("2026-07-20T10:05:00Z")).status)
        .toBe(StudyStartStatus.CONFLICT);
      expect(store.startTest(CardDirection.REVERSE, new Date("2026-07-20T10:06:00Z")).status)
        .toBe(StudyStartStatus.RESUMED);
      expect(store.activeMode()).toBe(StudyMode.TEST_REVERSE);
      expect(rows(state.storage, "SELECT id FROM study_sessions")).toHaveLength(1);
    });
  });

  it("keeps a tail retry out of the five-question denominator and the summary", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 5);
      const now = new Date("2026-07-20T10:00:00Z");
      const started = store.startTest(CardDirection.FORWARD, now);
      const sessionId = started.snapshot!.sessionId;

      const first = answerCurrent(store, ReviewRating.AGAIN, now, {
        grade: EvaluationGrade.INCORRECT,
        feedback: "Wrong.",
      });
      expect(first.result.snapshot!.progress).toEqual({ completed: 1, total: 5 });
      expect(first.result.snapshot!.queue).toHaveLength(6);

      for (let index = 0; index < 5; index += 1) {
        const outcome = answerCurrent(store, ReviewRating.GOOD, now);
        expect(outcome.result.status).toBe(FinalizeStatus.COMPLETED);
        expect(outcome.result.snapshot!.progress.total).toBe(5);
      }
      expect(store.activeMode()).toBeNull();
      expect(store.summary(sessionId)).toEqual({ correct: 4, partial: 0, incorrect: 1 });
      expect(
        rows<{ count: number }>(state.storage, "SELECT COUNT(*) AS count FROM review_attempts")[0]!
          .count,
      ).toBe(6);
    });
  });
});

describe("VocabularyStore session boundaries", () => {
  it("leaves unanswered cards due on exit and carries them into the next session", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 3);
      const now = new Date("2026-07-20T10:00:00Z");
      const started = store.startReview(now);
      const queued = started.snapshot!.queue.map((item) => item.card.id);
      const plan = store.currentPromptPlan(now)!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "Question?", now)!;
      store.recordDelivery(prompt.id, { deliveryId: "3", contentFingerprint: "e".repeat(64) });

      expect(store.exitStudy(new Date("2026-07-20T10:30:00Z"))).toBe(StudyMutationStatus.COMPLETED);
      expect(store.activeMode()).toBeNull();
      expect(store.answerablePrompt()).toBeNull();
      expect(
        rows<{ status: string }>(state.storage, "SELECT status FROM study_prompts WHERE id = ?", prompt.id)[0],
      ).toEqual({ status: "cancelled" });
      expect(
        rows<{ repetitions: number }>(
          state.storage,
          "SELECT repetitions FROM vocabulary_cards WHERE id IN (SELECT id FROM vocabulary_cards)",
        ).every(({ repetitions }) => repetitions === 0),
      ).toBe(true);

      const resumed = store.startReview(new Date("2026-07-20T11:00:00Z"));
      expect(resumed.status).toBe(StudyStartStatus.STARTED);
      expect(resumed.snapshot!.queue.map((item) => item.card.id)).toEqual(queued);
    });
  });

  it("reconciles a local-day rollover once and pins the in-flight delivered prompt", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      // UTC+8: 2026-07-20T02:00Z is local 10:00, 2026-07-20T17:00Z is the next day.
      const store = new VocabularyStore(state.storage, "Asia/Kuala_Lumpur");
      seedEntries(store, 3);
      const started = store.startReview(new Date("2026-07-20T02:00:00Z"));
      expect(started.snapshot!.localDate).toBe("2026-07-20");
      const second = started.snapshot!.queue[1]!;

      // Deliver the second queue item's prompt, then roll the local day over.
      Array.from(
        state.storage.sql.exec(
          "UPDATE study_queue SET status = 'queued' WHERE session_id = ? AND status = 'current'",
          started.snapshot!.sessionId,
        ),
      );
      Array.from(
        state.storage.sql.exec("UPDATE study_queue SET status = 'current' WHERE id = ?", second.id),
      );
      const plan = store.currentPromptPlan(new Date("2026-07-20T02:05:00Z"))!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "Pinned question?", new Date("2026-07-20T02:05:00Z"))!;
      store.recordDelivery(prompt.id, { deliveryId: "11", contentFingerprint: "f".repeat(64) });

      const rolled = store.snapshot(new Date("2026-07-20T17:00:00Z"))!;
      expect(rolled.localDate).toBe("2026-07-21");
      expect(rolled.queue[0]!.id).toBe(second.id);
      expect(rolled.queue[0]!.status).toBe(StudyQueueStatus.CURRENT);
      expect(rolled.currentPrompt?.id).toBe(prompt.id);
      expect(store.answerablePrompt()?.id).toBe(prompt.id);

      const again = store.snapshot(new Date("2026-07-20T18:00:00Z"))!;
      expect(again.queue.map((item) => item.id)).toEqual(rolled.queue.map((item) => item.id));
      expect(again.queue.map((item) => item.position)).toEqual(
        rolled.queue.map((item) => item.position),
      );
    });
  });

  it("retires a merely prepared prompt across a rollover and reselects its card", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "Asia/Kuala_Lumpur");
      seedEntries(store, 2);
      const started = store.startReview(new Date("2026-07-20T02:00:00Z"));
      const firstCardId = started.snapshot!.queue[0]!.card.id;
      const plan = store.currentPromptPlan(new Date("2026-07-20T02:05:00Z"))!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "Never delivered?", new Date("2026-07-20T02:05:00Z"))!;

      const rolled = store.snapshot(new Date("2026-07-20T17:00:00Z"))!;
      expect(rolled.localDate).toBe("2026-07-21");
      expect(
        rows<{ status: string }>(state.storage, "SELECT status FROM study_prompts WHERE id = ?", prompt.id)[0],
      ).toEqual({ status: "cancelled" });
      expect(rolled.currentPrompt).toBeNull();
      expect(rolled.queue.filter((item) => item.status !== StudyQueueStatus.SKIPPED)
        .map((item) => item.card.id)).toContain(firstCardId);
    });
  });

  it("reports due work that no prompt can answer", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 2);
      expect(store.dueButNotAnswerable(new Date("2026-07-20T10:00:00Z"))).toBe(false);
      makeDue(state.storage, 1, "2026-07-19T00:00:00Z");
      expect(store.dueButNotAnswerable(new Date("2026-07-20T10:00:00Z"))).toBe(true);
      expect(store.overdueBacklog(new Date("2026-07-20T10:00:00Z"))).toBe(true);
      expect(store.inFlightDelivery()).toBe(false);

      const now = new Date("2026-07-20T10:00:00Z");
      store.startReview(now);
      const plan = store.currentPromptPlan(now)!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "Question?", now)!;
      expect(store.inFlightDelivery()).toBe(false);
      store.recordDeliveryFailure(prompt.id, { error: "boom" });
      expect(store.inFlightDelivery()).toBe(false);

      store.recordDelivery(prompt.id, { deliveryId: "13", contentFingerprint: "0".repeat(64) });
      expect(store.dueButNotAnswerable(now)).toBe(false);
    });
  });
});
