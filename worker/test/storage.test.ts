import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import {
  CaptureStatus,
  EvaluationGrade,
  ReviewCompletionStatus,
  ReviewPromptStatus,
  TestCompletionStatus,
  TestSnapshotStatus,
  TestStartStatus,
} from "../src/domain/models";
import type { SenseCard } from "../src/domain/models";
import { VocabularyStore } from "../src/storage/vocabulary-store";

const CARD: SenseCard = {
  partOfSpeech: "noun",
  definition: "a definition",
  exampleSentence: "An example sentence.",
};

function stub() {
  return env.VOCABULARY.getByName(`storage-${crypto.randomUUID()}`);
}

describe("VocabularyStore", () => {
  it("initializes the effective v4 schema and preserves ordered capture aggregates", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage);
      const tables = Array.from(
        state.storage.sql.exec<{ name: string }>(
          "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        ),
      ).map(({ name }) => name);
      expect(tables).toEqual(
        expect.arrayContaining([
          "review_events",
          "test_questions",
          "test_sessions",
          "vocabulary_entries",
          "vocabulary_senses",
        ]),
      );
      const indexes = Array.from(
        state.storage.sql.exec<{ name: string }>(
          "SELECT name FROM sqlite_master WHERE type = 'index' ORDER BY name",
        ),
      ).map(({ name }) => name);
      expect(indexes).toEqual(
        expect.arrayContaining([
          "one_active_test_session_idx",
          "review_events_entry_id_idx",
          "test_questions_session_position_idx",
          "vocabulary_review_order_idx",
          "vocabulary_senses_entry_order_idx",
        ]),
      );

      const result = store.captureEntry(
        "  Straße  ",
        [
          CARD,
          { partOfSpeech: "verb", definition: "to define", exampleSentence: "I define it." },
        ],
        new Date("2026-07-20T00:00:00Z"),
      );
      expect(result.status).toBe(CaptureStatus.SAVED);
      expect(result.entry?.normalizedText).toBe("strasse");
      expect(result.entry?.senses.map(({ partOfSpeech }) => partOfSpeech)).toEqual(["noun", "verb"]);
      expect(store.captureEntry("STRASSE", [CARD]).status).toBe(CaptureStatus.ALREADY_EXISTS);
      expect(
        store.captureEntry("invalid", [CARD, { ...CARD, partOfSpeech: " Noun " }]).status,
      ).toBe(CaptureStatus.INVALID);
    });
  });

  it("creates one local-day review, marks older pending only on a later review, and uses CAS", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage);
      store.captureEntry("first", [CARD], new Date("2026-07-19T00:00:00Z"));
      store.captureEntry("second", [CARD], new Date("2026-07-20T00:00:00Z"));

      const first = store.dailyReview(new Date("2026-07-20T16:30:00Z"));
      expect(first.status).toBe(ReviewPromptStatus.PENDING);
      expect(first.event?.reviewDate).toBe("2026-07-21");
      expect(store.dailyReview(new Date("2026-07-21T10:00:00Z")).event?.id).toBe(first.event?.id);

      const later = store.dailyReview(new Date("2026-07-21T16:30:00Z"));
      expect(later.status).toBe(ReviewPromptStatus.PENDING);
      expect(later.event?.reviewDate).toBe("2026-07-22");
      expect(
        state.storage.sql
          .exec<{ status: string }>("SELECT status FROM review_events WHERE id = ?", first.event!.id)
          .one().status,
      ).toBe("missed");

      const completion = store.completeReview(
        later.event!.id,
        "raw answer",
        { grade: EvaluationGrade.PARTIAL, feedback: "Close." },
        new Date("2026-07-21T17:00:00Z"),
      );
      expect(completion.status).toBe(ReviewCompletionStatus.COMPLETED);
      expect(completion.answerText).toBe("raw answer");
      expect(store.completeReview(later.event!.id, "again", {
        grade: EvaluationGrade.CORRECT,
        feedback: "No.",
      }).status).toBe(ReviewCompletionStatus.NO_PENDING);
    });
  });

  it("runs an exact five-question test with stale protection and no ordinary review mutation", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage);
      for (let index = 1; index <= 6; index += 1) {
        store.captureEntry(`entry-${index}`, [CARD], new Date(`2026-07-${10 + index}T00:00:00Z`));
      }
      const started = store.startTest(new Date("2026-07-20T00:00:00Z"));
      expect(started.status).toBe(TestStartStatus.STARTED);
      expect(started.snapshot?.questions).toHaveLength(5);
      expect(store.startTest().status).toBe(TestStartStatus.RESUMED);
      const firstQuestion = started.snapshot!.currentQuestion!;
      expect(
        store.completeTest(firstQuestion.id + 1000, "answer", {
          grade: EvaluationGrade.INCORRECT,
          feedback: "Wrong.",
        }).status,
      ).toBe(TestCompletionStatus.STALE);

      let snapshot = store.currentTest();
      for (let index = 0; index < 5; index += 1) {
        expect(snapshot.status).toBe(TestSnapshotStatus.ACTIVE);
        const question = snapshot.snapshot!.currentQuestion!;
        const result = store.completeTest(
          question.id,
          `answer ${index}`,
          {
            grade: index === 0 ? EvaluationGrade.CORRECT : EvaluationGrade.INCORRECT,
            feedback: "Recorded.",
          },
          new Date(`2026-07-20T00:0${index}:00Z`),
        );
        expect(result.status).toBe(
          index === 4 ? TestCompletionStatus.COMPLETED : TestCompletionStatus.ADVANCED,
        );
        if (index === 4) {
          expect(result.snapshot?.summary).toEqual({ correct: 1, partial: 0, incorrect: 4 });
        }
        snapshot = store.currentTest();
      }
      expect(snapshot.status).toBe(TestSnapshotStatus.NONE);
      expect(
        Array.from(
          state.storage.sql.exec<{ count: number }>(
            "SELECT COUNT(*) AS count FROM vocabulary_entries WHERE last_reviewed IS NOT NULL",
          ),
        )[0]?.count,
      ).toBe(0);
    });
  });

  it("rolls back review and test mutations when prepared-response persistence fails", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const store = new VocabularyStore(state.storage);
      for (let index = 1; index <= 6; index += 1) {
        store.captureEntry(`atomic-${index}`, [CARD], new Date(`2026-07-${10 + index}T00:00:00Z`));
      }

      const review = store.dailyReview(new Date("2026-07-20T16:30:00Z"));
      expect(
        store.completeReview(review.event!.id, "\u001c\u001d", {
          grade: EvaluationGrade.CORRECT,
          feedback: "Correct.",
        }).status,
      ).toBe(ReviewCompletionStatus.INVALID);
      expect(store.pendingReview().event?.id).toBe(review.event?.id);
      const reviewResult = store.completeReview(
        review.event!.id,
        "answer",
        { grade: EvaluationGrade.CORRECT, feedback: "Correct." },
        new Date("2026-07-20T17:00:00Z"),
        () => {
          throw new Error("prepared response failed");
        },
      );
      expect(reviewResult.status).toBe(ReviewCompletionStatus.STORAGE_ERROR);
      expect(store.pendingReview().event?.id).toBe(review.event?.id);
      expect(
        store.completeReview(review.event!.id, "answer", {
          grade: EvaluationGrade.CORRECT,
          feedback: "Correct.",
        }).status,
      ).toBe(ReviewCompletionStatus.COMPLETED);

      const started = store.startTest(new Date("2026-07-21T00:00:00Z"));
      const question = started.snapshot!.currentQuestion!;
      expect(
        store.completeTest(question.id, "\u001e\u001f", {
          grade: EvaluationGrade.CORRECT,
          feedback: "Correct.",
        }).status,
      ).toBe(TestCompletionStatus.INVALID);
      expect(store.currentTest().snapshot?.currentQuestion?.id).toBe(question.id);
      const testResult = store.completeTest(
        question.id,
        "answer",
        { grade: EvaluationGrade.CORRECT, feedback: "Correct." },
        new Date("2026-07-21T00:01:00Z"),
        () => {
          throw new Error("prepared response failed");
        },
      );
      expect(testResult.status).toBe(TestCompletionStatus.STORAGE_ERROR);
      expect(store.currentTest().snapshot?.currentQuestion?.id).toBe(question.id);
    });
  });

});
