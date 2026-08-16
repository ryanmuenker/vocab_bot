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
import { initializeSchema } from "../src/storage/schema";
import { VocabularyStore, localMidnightUtc } from "../src/storage/vocabulary-store";

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

const DIVERSE_CARDS: readonly SenseCard[] = [
  {
    partOfSpeech: "noun",
    definition: "a place that stores money for customers",
    exampleSentence: "She deposited her pay at the bank.",
  },
  {
    partOfSpeech: "noun",
    definition: "a business that stores money for customers",
    exampleSentence: "The bank approved the loan.",
  },
  {
    partOfSpeech: "noun",
    definition: "the sloping land beside a river",
    exampleSentence: "They picnicked on the river bank.",
  },
  {
    partOfSpeech: "verb",
    definition: "to tilt an aircraft during a turn",
    exampleSentence: "The pilot banked left.",
  },
  {
    partOfSpeech: "noun",
    definition: "an organization that stores money for customers",
    exampleSentence: "The bank safeguards deposits.",
  },
];
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
  it("keeps every sense but projects one forward and at most three diverse reverse cards atomically", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      const result = store.captureEntry(
        "  Bank  ",
        DIVERSE_CARDS,
        new Date("2026-07-20T00:00:00Z"),
      );

      expect(result.status).toBe(CaptureStatus.SAVED);
      expect(result.entry?.normalizedText).toBe("bank");
      expect(result.entry?.senses).toHaveLength(5);
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
        ["reverse", result.entry!.senses[2]!.id],
        ["reverse", result.entry!.senses[3]!.id],
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

  it("introduces a new word through its forward card before reverse siblings", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      const result = store.captureEntry(
        "bank",
        DIVERSE_CARDS,
        new Date("2026-07-20T09:00:00Z"),
      );

      const started = store.startReview(new Date("2026-07-20T10:00:00Z"));

      expect(started.status).toBe(StudyStartStatus.STARTED);
      expect(started.snapshot!.queue.map((item) => [
        item.card.direction,
        item.card.senseId,
      ])).toEqual([["forward", null]]);
      expect(result.entry?.senses).toHaveLength(5);
    });
  });

  it("runs legacy reverse pruning once without deleting a queued extra", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      const result = store.captureEntry(
        "bank",
        DIVERSE_CARDS,
        new Date("2026-07-20T00:00:00Z"),
      );
      const senses = result.entry!.senses;
      const legacySenseIds = [senses[1]!.id, senses[4]!.id];
      Array.from(
        state.storage.sql.exec(
          `DELETE FROM vocabulary_cards
           WHERE direction = 'reverse' AND sense_id IN (?, ?)`,
          ...legacySenseIds,
        ),
      );
      for (const senseId of legacySenseIds) {
        Array.from(
          state.storage.sql.exec(
            `INSERT INTO vocabulary_cards (
               entry_id, sense_id, direction, state, stability, difficulty,
               due_at, effective_due_at, last_review_at, repetitions, lapses,
               scheduler_kind, scheduler_version, parameters_version,
               parameter_fingerprint, desired_retention,
               introduced_local_date, buried_until_local_date, created_at
             )
             SELECT entry_id, ?, 'reverse', state, stability, difficulty,
                    due_at, effective_due_at, last_review_at, repetitions, lapses,
                    scheduler_kind, scheduler_version, parameters_version,
                    parameter_fingerprint, desired_retention,
                    introduced_local_date, buried_until_local_date, created_at
             FROM vocabulary_cards
             WHERE entry_id = ? AND direction = 'forward'`,
            senseId,
            result.entry!.id,
          ),
        );
      }
      const queuedCardId = rows<{ id: number }>(
        state.storage,
        "SELECT id FROM vocabulary_cards WHERE sense_id = ?",
        senses[4]!.id,
      )[0]!.id;
      Array.from(
        state.storage.sql.exec(
          `INSERT INTO study_sessions
             (mode, status, started_at, completed_at, local_date)
           VALUES ('review', 'exited', ?, ?, '2026-07-20')`,
          "2026-07-20T01:00:00Z",
          "2026-07-20T02:00:00Z",
        ),
      );
      Array.from(
        state.storage.sql.exec(
          `INSERT INTO study_queue
             (session_id, card_id, position, status, introduced_local_date)
           VALUES (last_insert_rowid(), ?, 1, 'skipped', '2026-07-20')`,
          queuedCardId,
        ),
      );

      expect(store.runReverseCardCapMaintenance()).toBe(2);
      expect(store.runReverseCardCapMaintenance()).toBe(0);

      expect(
        rows<{ sense_id: number }>(
          state.storage,
          `SELECT sense_id FROM vocabulary_cards
           WHERE entry_id = ? AND direction = 'reverse'
           ORDER BY sense_id`,
          result.entry!.id,
        ).map(({ sense_id }) => sense_id),
      ).toEqual([senses[0]!.id, senses[3]!.id, senses[4]!.id]);
      expect(result.entry!.senses).toHaveLength(5);
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

  it("introduces ten distinct unseen entries per local day across sessions", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 16);
      const expectedCardIds = Array.from({ length: 10 }, (_, index) => index * 2 + 1);

      const started = store.startReview(new Date("2026-07-20T10:00:00Z"));
      expect(started.snapshot!.queue.map((item) => item.card.id)).toEqual(expectedCardIds);
      expect(new Set(started.snapshot!.queue.map((item) => item.card.entryId)).size).toBe(10);
      expect(
        rows<{ count: number }>(
          state.storage,
          "SELECT COUNT(DISTINCT entry_id) AS count FROM vocabulary_cards " +
            "WHERE introduced_local_date = '2026-07-20'",
        )[0]!.count,
      ).toBe(10);
      expect(
        rows<{ id: number }>(
          state.storage,
          "SELECT id FROM vocabulary_cards " +
            "WHERE introduced_local_date = '2026-07-20' ORDER BY id",
        ).map(({ id }) => id),
      ).toEqual(expectedCardIds);

      expect(store.exitStudy(new Date("2026-07-20T10:05:00Z"))).toBe(
        StudyMutationStatus.COMPLETED,
      );
      const second = store.startReview(new Date("2026-07-20T11:00:00Z"));
      expect(second.snapshot!.queue.map((item) => item.card.id)).toEqual(expectedCardIds);
    });
  });

  it("repairs unqueued siblings incorrectly marked as introduced", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      store.captureEntry("polyseme", [CARD, SECOND_CARD], new Date("2026-07-20T09:00:00Z"));
      const started = store.startReview(new Date("2026-07-20T10:00:00Z"));
      expect(started.snapshot!.queue.map((item) => item.card.id)).toEqual([1]);

      Array.from(
        state.storage.sql.exec(
          `UPDATE vocabulary_cards
           SET introduced_local_date = '2026-07-20'
           WHERE entry_id = 1`,
        ),
      );

      initializeSchema(state.storage.sql);
      expect(
        rows<{ introduced_local_date: string | null }>(
          state.storage,
          `SELECT introduced_local_date FROM vocabulary_cards
           WHERE entry_id = 1 ORDER BY id`,
        ),
      ).toEqual([
        { introduced_local_date: "2026-07-20" },
        { introduced_local_date: "2026-07-20" },
        { introduced_local_date: "2026-07-20" },
      ]);

      expect(store.repairUnqueuedIntroductions()).toBe(2);
      expect(
        rows<{ id: number; introduced_local_date: string | null }>(
          state.storage,
          `SELECT id, introduced_local_date
           FROM vocabulary_cards
           WHERE entry_id = 1
           ORDER BY id`,
        ),
      ).toEqual([
        { id: 1, introduced_local_date: "2026-07-20" },
        { id: 2, introduced_local_date: null },
        { id: 3, introduced_local_date: null },
      ]);
    });
  });

  it("introduces every sense card on later days after sibling burial", async () => {
    await runInDurableObject(stub(), (_instance, _state) => {
      const store = new VocabularyStore(_state.storage, "UTC");
      store.captureEntry("polyseme", [CARD, SECOND_CARD], new Date("2026-07-19T09:00:00Z"));

      const firstDay = new Date("2026-07-20T10:00:00Z");
      const first = store.startReview(firstDay);
      expect(first.snapshot!.queue.map((item) => item.card.id)).toEqual([1]);
      answerCurrent(store, ReviewRating.GOOD, firstDay);

      const secondDay = new Date("2026-07-21T10:00:00Z");
      const second = store.startReview(secondDay);
      expect(second.snapshot!.queue.map((item) => item.card.id)).toEqual([2]);
      answerCurrent(store, ReviewRating.GOOD, secondDay);

      const seen = new Set([1, 2]);
      for (let day = 22; day <= 31 && !seen.has(3); day += 1) {
        const now = new Date(`2026-07-${day}T10:00:00Z`);
        const started = store.startReview(now);
        const cardId = started.snapshot!.queue[0]!.card.id;
        seen.add(cardId);
        if (cardId !== 3) answerCurrent(store, ReviewRating.GOOD, now);
      }
      expect(seen).toEqual(new Set([1, 2, 3]));
    });
  });

  it("prioritizes unseen siblings before untouched entries", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      store.captureEntry("attempted", [CARD], new Date("2026-05-01T00:00:00Z"));
      const firstDay = new Date("2026-07-20T10:00:00Z");
      store.startReview(firstDay);
      answerCurrent(store, ReviewRating.GOOD, firstDay);

      // Reproduce a legacy/prod entry whose reviewed forward card predates an
      // unintroduced reverse sibling.
      Array.from(
        state.storage.sql.exec(
          `UPDATE vocabulary_cards
           SET introduced_local_date = NULL, buried_until_local_date = NULL
           WHERE entry_id = 1 AND direction = 'reverse'`,
        ),
      );
      seedEntries(store, 10, "untouched");

      const started = store.startReview(new Date("2026-07-21T10:00:00Z"));
      expect(started.status).toBe(StudyStartStatus.STARTED);
      expect(started.snapshot!.queue.map((item) => item.card.entryId)).toEqual(
        Array.from({ length: 10 }, (_, index) => index + 1),
      );
    });
  });

  it("does not let directional tests consume daily review introductions", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 20);
      const now = new Date("2026-07-20T10:00:00Z");

      const test = store.startTest(CardDirection.FORWARD, now);
      expect(test.snapshot!.queue.map((item) => item.card.entryId)).toEqual([1, 2, 3, 4, 5]);
      expect(store.exitStudy(now)).toBe(StudyMutationStatus.COMPLETED);

      const review = store.startReview(now);
      expect(review.snapshot!.queue.map((item) => item.card.entryId)).toEqual(
        Array.from({ length: 10 }, (_, index) => index + 1),
      );
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

      // The reverse sibling of the same entry was not selected into this
      // distinct-entry queue, and finalization still buries it for the day.
      const sibling = rows<{ buried_until_local_date: string | null; effective_due_at: string }>(
        state.storage,
        "SELECT * FROM vocabulary_cards WHERE id = 2",
      )[0]!;
      expect(sibling.buried_until_local_date).toBe("2026-07-20");
      expect(sibling.effective_due_at).toBe("2026-06-01T00:00:00Z");
      const queue = result.snapshot!.queue;
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

  it("does not append a same-session retry in a daily review", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      store.captureEntry("obdurate", [CARD], new Date("2026-06-01T00:00:00Z"));
      const now = new Date("2026-07-20T10:00:00Z");
      store.startReview(now);

      const outcome = answerCurrent(store, ReviewRating.AGAIN, now, {
        grade: EvaluationGrade.INCORRECT,
        feedback: "Wrong.",
      });

      expect(outcome.result.transition!.retrySameSession).toBe(false);
      expect(outcome.result.transition!.effectiveDue.getTime())
        .toBe(outcome.result.transition!.rawDue.getTime());
      expect(outcome.result.transition!.effectiveDue.getTime())
        .toBeGreaterThanOrEqual(Date.parse("2026-07-21T10:00:00Z"));
      expect(outcome.result.snapshot!.queue.filter(
        (item) => item.retryOfQueueItemId !== null,
      )).toHaveLength(0);
      expect(outcome.result.snapshot!.status).toBe("completed");
      expect(
        rows<{ is_same_session_retry: number }>(
          state.storage,
          "SELECT is_same_session_retry FROM review_attempts",
        ),
      ).toEqual([{ is_same_session_retry: 0 }]);
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

  it("selects only unseen entries and bypasses the daily quota for explicit tests", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "UTC");
      seedEntries(store, 10);
      for (const cardId of [1, 3, 5, 7, 9]) {
        makeDue(state.storage, cardId, "2026-07-25T00:00:00Z");
      }
      Array.from(
        state.storage.sql.exec(
          `UPDATE vocabulary_cards
           SET introduced_local_date = '2026-07-20'
           WHERE id IN (1, 3, 5, 7, 9)`,
        ),
      );

      const started = store.startTest(
        CardDirection.FORWARD,
        new Date("2026-07-20T10:00:00Z"),
      );

      expect(started.status).toBe(StudyStartStatus.STARTED);
      expect(started.snapshot!.queue.map((item) => item.card.id)).toEqual([
        11, 13, 15, 17, 19,
      ]);
      expect(
        started.snapshot!.queue.every((item) => item.card.state === "new"),
      ).toBe(true);
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
      expect(
        rows<{
          rating: string;
          is_same_session_retry: number;
          retry_of_attempt_id: number | null;
        }>(
          state.storage,
          `SELECT rating, is_same_session_retry, retry_of_attempt_id
           FROM review_attempts
           WHERE retry_of_attempt_id IS NOT NULL`,
        ),
      ).toEqual([{
        rating: ReviewRating.GOOD,
        is_same_session_retry: 1,
        retry_of_attempt_id: 1,
      }]);
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

  it("keeps rollover queues distinct by vocabulary entry", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "Asia/Kuala_Lumpur");
      store.captureEntry("polyseme", [CARD, SECOND_CARD], new Date("2026-06-01T00:00:00Z"));
      store.captureEntry("other", [CARD], new Date("2026-06-02T00:00:00Z"));
      for (const cardId of [1, 2, 3, 4, 5]) {
        makeDue(state.storage, cardId, "2026-07-19T00:00:00Z");
      }

      const started = store.startReview(new Date("2026-07-20T02:00:00Z"));
      expect(started.snapshot!.queue.map((item) => item.card.entryId)).toEqual([1, 2]);

      const rolled = store.snapshot(new Date("2026-07-20T17:00:00Z"))!;
      expect(
        rolled.queue
          .filter((item) => item.status !== StudyQueueStatus.SKIPPED)
          .map((item) => item.card.entryId),
      ).toEqual([1, 2]);
    });
  });

  it("continues a review when the first rating lands after local midnight", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage, "Asia/Kuala_Lumpur");
      seedEntries(store, 3);
      const beforeMidnight = new Date("2026-07-20T15:55:00Z");
      const afterMidnight = new Date("2026-07-20T16:05:00Z");
      const started = store.startReview(beforeMidnight);
      const plan = store.currentPromptPlan(beforeMidnight)!;
      const prompt = store.prepareCurrentPrompt(plan.promptKey, "Question?", beforeMidnight)!;
      store.recordDelivery(prompt.id, {
        deliveryId: "midnight-1",
        contentFingerprint: "a".repeat(64),
        now: beforeMidnight,
      });
      store.recordAnswer(prompt.id, "an answer", CORRECT, beforeMidnight);

      const finalized = store.finalize(prompt.id, ReviewRating.EASY, afterMidnight);
      const continued = store.currentPromptPlan(afterMidnight);

      expect(finalized.status).toBe(FinalizeStatus.COMPLETED);
      expect(finalized.snapshot?.progress).toEqual({ completed: 1, total: 3 });
      expect(continued).not.toBeNull();
      expect(continued?.snapshot.localDate).toBe("2026-07-21");
      expect(continued?.snapshot.progress).toEqual({ completed: 1, total: 3 });
      expect(continued?.context.queueItem.card.id).not.toBe(plan.context.queueItem.card.id);
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

describe("localMidnightUtc", () => {
  it("resolves fractional-offset zones exactly, matching Python's ZoneInfo", () => {
    expect(localMidnightUtc("2026-07-24", "Asia/Kolkata").toISOString()).toBe(
      "2026-07-23T18:30:00.000Z",
    );
    expect(localMidnightUtc("2026-07-24", "Asia/Kathmandu").toISOString()).toBe(
      "2026-07-23T18:15:00.000Z",
    );
    expect(localMidnightUtc("2026-07-24", "America/St_Johns").toISOString()).toBe(
      "2026-07-24T02:30:00.000Z",
    );
  });

  it("keeps whole-hour zones correct across DST", () => {
    expect(localMidnightUtc("2026-07-24", "UTC").toISOString()).toBe("2026-07-24T00:00:00.000Z");
    expect(localMidnightUtc("2026-07-24", "America/New_York").toISOString()).toBe(
      "2026-07-24T04:00:00.000Z",
    );
    expect(localMidnightUtc("2026-01-24", "America/New_York").toISOString()).toBe(
      "2026-01-24T05:00:00.000Z",
    );
  });
});
