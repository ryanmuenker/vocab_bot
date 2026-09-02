import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import {
  MEMORIZED_STABILITY_DAYS,
  type InspectorData,
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

  it("reports bounded image counts without exposing Telegram IDs or provider errors", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      state.storage.sql.exec(`
        INSERT INTO vocabulary_entries
          (id, display_text, normalized_text, date_added, last_reviewed, review_status)
        VALUES
          (1, 'Amphora', 'amphora', '2026-08-01T00:00:00Z', NULL, 'new'),
          (2, 'Corbel', 'corbel', '2026-08-02T00:00:00Z', NULL, 'new'),
          (3, 'Serein', 'serein', '2026-08-03T00:00:00Z', NULL, 'new');

        INSERT INTO vocabulary_senses
          (id, entry_id, definition, part_of_speech, example_sentence, source_context, date_added)
        VALUES
          (1, 1, 'a tall ancient jar', 'noun', 'The amphora stood by the wall.', NULL, '2026-08-01T00:00:00Z'),
          (2, 2, 'a projecting stone bracket', 'noun', 'The corbel supported the arch.', NULL, '2026-08-02T00:00:00Z'),
          (3, 3, 'fine evening rain', 'noun', 'A serein settled over the field.', NULL, '2026-08-03T00:00:00Z');

        INSERT INTO vocabulary_entry_images
          (id, entry_id, sense_id, category, query, description, photo_url, caption,
           source_url, telegram_file_id, telegram_file_unique_id, origin, created_at, updated_at)
        VALUES
          (1, 1, 1, 'object', 'ancient amphora', 'A terracotta amphora',
           'https://upload.example/amphora.jpg', 'An ancient amphora',
           'https://commons.example/amphora', 'telegram-sensitive-file-id',
           'telegram-sensitive-unique-id', 'capture',
           '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z'),
          (2, 2, 2, 'architecture', 'stone corbel', 'A carved stone corbel',
           NULL, NULL, NULL, NULL, NULL, 'backfill',
           '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z');

        INSERT INTO image_backfill_attempts
          (id, entry_id, status, attempt_count, last_error, attempted_at)
        VALUES
          (1, 3, 'provider_error', 2, 'provider-sensitive-error-detail',
           '2026-08-03T00:00:00Z');
      `);

      const serialized = JSON.stringify(
        readInspectorData(state.storage, "2026-08-14T00:00:00Z"),
      );
      const result = JSON.parse(serialized) as InspectorData;

      expect(result.images).toEqual({
        associated: 2,
        telegramCached: 1,
        unresolvedCandidates: 1,
        backfillFailures: {
          no_visual: 0,
          provider_error: 1,
          rate_limited: 0,
          invalid_response: 0,
          image_unavailable: 0,
        },
      });
      expect(serialized).not.toContain("telegram-sensitive-file-id");
      expect(serialized).not.toContain("telegram-sensitive-unique-id");
      expect(serialized).not.toContain("provider-sensitive-error-detail");
      expect(serialized).not.toContain("telegram_file_id");
      expect(serialized).not.toContain("last_error");
    });
  });
});
