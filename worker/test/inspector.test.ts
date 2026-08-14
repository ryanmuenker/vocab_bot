import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import {
  MEMORIZED_STABILITY_DAYS,
  readInspectorData,
} from "../src/domain/inspector";

function stub() {
  return env.VOCABULARY.getByName(`inspector-${crypto.randomUUID()}`);
}

describe("vocabulary inspector projection", () => {
  it("projects live vocabulary state into unseen, learning, and memorized entries", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      state.storage.sql.exec(`
        INSERT INTO vocabulary_entries
          (id, display_text, normalized_text, date_added, last_reviewed, review_status)
        VALUES
          (1, 'Lachrymose', 'lachrymose', '2026-08-01T00:00:00Z', NULL, 'new'),
          (2, 'Obdurate', 'obdurate', '2026-07-01T00:00:00Z', '2026-08-13T00:00:00Z', 'reviewed'),
          (3, 'Pro forma', 'pro forma', '2026-06-01T00:00:00Z', '2026-08-12T00:00:00Z', 'reviewed');

        INSERT INTO vocabulary_senses
          (id, entry_id, definition, part_of_speech, example_sentence, source_context, date_added)
        VALUES
          (1, 1, 'tearful or given to weeping', 'adjective', 'A lachrymose farewell.', NULL, '2026-08-01T00:00:00Z'),
          (2, 2, 'stubbornly refusing to change', 'adjective', 'An obdurate refusal.', 'reading', '2026-07-01T00:00:00Z'),
          (3, 3, 'done as a matter of form', 'adjective', 'A pro forma review.', NULL, '2026-06-01T00:00:00Z'),
          (4, 3, 'projected from assumptions', 'adjective', 'Pro forma results.', NULL, '2026-06-01T00:00:00Z');

        INSERT INTO vocabulary_cards
          (id, entry_id, sense_id, direction, state, stability, difficulty, due_at,
           effective_due_at, last_review_at, repetitions, lapses, scheduler_kind,
           scheduler_version, parameters_version, parameter_fingerprint,
           desired_retention, introduced_local_date, buried_until_local_date, created_at)
        VALUES
          (1, 1, NULL, 'forward', 'new', NULL, NULL, '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', NULL, 0, 0, 'fsrs', '6', 'fsrs-6-default', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0.9, NULL, NULL, '2026-08-01T00:00:00Z'),
          (2, 1, 1, 'reverse', 'new', NULL, NULL, '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', NULL, 0, 0, 'fsrs', '6', 'fsrs-6-default', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0.9, NULL, NULL, '2026-08-01T00:00:00Z'),
          (3, 2, NULL, 'forward', 'review', 45.0, 5.0, '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z', '2026-08-13T00:00:00Z', 4, 0, 'fsrs', '6', 'fsrs-6-default', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0.9, '2026-07-01', NULL, '2026-07-01T00:00:00Z'),
          (4, 2, 2, 'reverse', 'review', 29.9, 5.0, '2026-08-14T00:00:00Z', '2026-08-14T00:00:00Z', '2026-08-12T00:00:00Z', 2, 1, 'fsrs', '6', 'fsrs-6-default', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0.9, '2026-07-01', NULL, '2026-07-01T00:00:00Z'),
          (5, 3, NULL, 'forward', 'review', 45.0, 4.0, '2026-09-25T00:00:00Z', '2026-09-25T00:00:00Z', '2026-08-12T00:00:00Z', 3, 0, 'fsrs', '6', 'fsrs-6-default', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0.9, '2026-06-01', NULL, '2026-06-01T00:00:00Z'),
          (6, 3, 3, 'reverse', 'review', 30.0, 4.0, '2026-09-10T00:00:00Z', '2026-09-10T00:00:00Z', '2026-08-11T00:00:00Z', 3, 0, 'fsrs', '6', 'fsrs-6-default', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0.9, '2026-06-01', NULL, '2026-06-01T00:00:00Z'),
          (7, 3, 4, 'reverse', 'review', 36.0, 4.0, '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z', '2026-08-11T00:00:00Z', 3, 0, 'fsrs', '6', 'fsrs-6-default', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0.9, '2026-06-01', NULL, '2026-06-01T00:00:00Z');
      `);

      for (let id = 1; id <= 4; id += 1) {
        const reviewedAt = `2026-08-${String(9 + id).padStart(2, "0")}T00:00:00Z`;
        state.storage.sql.exec(
          `INSERT INTO review_attempts
            (id, card_id, source, rating, submitted_answer, evaluator_grade,
             evaluation_feedback, reviewed_at, before_state, before_stability,
             before_difficulty, before_due_at, before_effective_due_at,
             before_last_review_at, before_repetitions, before_lapses, after_state,
             after_stability, after_difficulty, after_raw_due_at,
             after_effective_due_at, after_last_review_at, after_repetitions,
             after_lapses, scheduler_kind, scheduler_version, parameters_version,
             parameter_fingerprint, desired_retention, is_same_session_retry,
             retry_of_attempt_id, legacy_source, legacy_id, created_at)
           VALUES (?, 3, 'review', ?, 'answer', 'correct', 'Accurate.', ?,
             'review', 20.0, 5.0, '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z',
             '2026-08-01T00:00:00Z', ?, 0, 'review', 45.0, 5.0,
             '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z', ?, ?, 0,
             'fsrs', '6', 'fsrs-6-default',
             'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
             0.9, 0, NULL, NULL, NULL, ?)`,
          id,
          id === 4 ? "easy" : "good",
          reviewedAt,
          id - 1,
          reviewedAt,
          id,
          reviewedAt,
        );
      }

      const result = readInspectorData(state.storage, "2026-08-14T00:00:00.500Z");

      expect(MEMORIZED_STABILITY_DAYS).toBe(30);
      expect(result.summary).toEqual({
        total: 3,
        unseen: 1,
        learning: 1,
        memorized: 1,
        due: 2,
      });
      expect(result.generatedAt).toBe("2026-08-14T00:00:00.500Z");
      expect(result.memorizedStabilityDays).toBe(30);

      expect(result.entries.map((entry) => ({
        displayText: entry.displayText,
        status: entry.status,
        due: entry.due,
        weakestStability: entry.weakestStability,
      }))).toEqual([
        { displayText: "Lachrymose", status: "unseen", due: true, weakestStability: null },
        { displayText: "Obdurate", status: "learning", due: true, weakestStability: 29.9 },
        { displayText: "Pro forma", status: "memorized", due: false, weakestStability: 30 },
      ]);

      expect(result.entries[2]!.senses.map((sense) => sense.definition)).toEqual([
        "done as a matter of form",
        "projected from assumptions",
      ]);
      expect(result.entries[2]!.cards).toHaveLength(3);
      expect(result.entries[1]!.recentAttempts.map((attempt) => attempt.id)).toEqual([4, 3, 2]);
      expect(result.entries[1]!.recentAttempts[0]).toMatchObject({
        rating: "easy",
        evaluatorGrade: "correct",
        source: "review",
        reviewedAt: "2026-08-13T00:00:00Z",
      });
    });
  });
});
