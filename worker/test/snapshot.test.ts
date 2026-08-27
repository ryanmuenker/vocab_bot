import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import {
  canonicalizeJcs,
  encodeReal,
  parseSnapshot,
  parseVerifiedEnvelope,
  readSnapshot,
  sha256Snapshot,
  summarizeSnapshot,
  writeSnapshot,
  type SnapshotV2,
} from "../src/domain/snapshot";
import { initializeSchema } from "../src/storage/schema";

/** Exercises every v5 table, both card directions, and preserved v4 history. */
const SNAPSHOT = {
  formatVersion: 2,
  entries: [{
    id: 7,
    displayText: "Straße 😀",
    normalizedText: "strasse 😀",
    dateAdded: "2026-07-20T00:00:00Z",
    lastReviewed: "2026-07-22T16:30:00Z",
    reviewStatus: "reviewed",
  }],
  senses: [
    {
      id: 11,
      entryId: 7,
      definition: "café \"quote\" \\ slash\u2028line",
      partOfSpeech: "noun",
      exampleSentence: "Control-safe example.",
      sourceContext: "source",
      dateAdded: "2026-07-20T00:00:00Z",
    },
    {
      id: 12,
      entryId: 7,
      definition: "a second sense",
      partOfSpeech: "verb",
      exampleSentence: "I define it.",
      sourceContext: null,
      dateAdded: "2026-07-20T00:00:00Z",
    },
  ],
  reviewEvents: [{
    id: 3,
    entryId: 7,
    reviewDate: "2026-07-22",
    status: "answered",
    promptedAt: "2026-07-22T16:00:00Z",
    answeredAt: "2026-07-22T16:30:00Z",
    answerText: "an answer",
    grade: "partial",
    evaluationFeedback: "close enough",
  }],
  testSessions: [{
    id: 2,
    status: "completed",
    startedAt: "2026-07-21T17:00:00Z",
    completedAt: "2026-07-21T17:30:00Z",
  }],
  testQuestions: [{
    id: 5,
    sessionId: 2,
    entryId: 7,
    position: 1,
    answerText: "a test answer",
    grade: "correct",
    evaluationFeedback: "exactly right",
    answeredAt: "2026-07-21T17:20:00Z",
  }],
  cards: [
    {
      id: 20,
      entryId: 7,
      senseId: null,
      direction: "forward",
      state: "review",
      stability: "3.2173",
      difficulty: "5.0",
      dueAt: "2026-07-25T16:30:00Z",
      effectiveDueAt: "2026-07-26T00:00:00Z",
      lastReviewAt: "2026-07-22T16:30:00Z",
      repetitions: 1,
      lapses: 0,
      schedulerKind: "fsrs",
      schedulerVersion: "6",
      parametersVersion: "fsrs-6-default",
      parameterFingerprint: "a".repeat(64),
      desiredRetention: "0.9",
      introducedLocalDate: "2026-07-20",
      buriedUntilLocalDate: null,
      createdAt: "2026-07-20T00:00:00Z",
    },
    {
      id: 21,
      entryId: 7,
      senseId: 11,
      direction: "reverse",
      state: "new",
      stability: null,
      difficulty: null,
      dueAt: "2026-07-20T00:00:00Z",
      effectiveDueAt: "2026-07-20T00:00:00Z",
      lastReviewAt: null,
      repetitions: 0,
      lapses: 0,
      schedulerKind: "fsrs",
      schedulerVersion: "6",
      parametersVersion: "fsrs-6-default",
      parameterFingerprint: "a".repeat(64),
      desiredRetention: "0.9",
      introducedLocalDate: null,
      buriedUntilLocalDate: "2026-07-24",
      createdAt: "2026-07-20T00:00:00Z",
    },
  ],
  studySessions: [{
    id: 30,
    mode: "review",
    status: "completed",
    startedAt: "2026-07-22T16:00:00Z",
    completedAt: "2026-07-22T16:35:00Z",
    localDate: "2026-07-22",
    legacyTestSessionId: 2,
  }],
  studyQueue: [
    {
      id: 40,
      sessionId: 30,
      cardId: 20,
      position: 1,
      status: "completed",
      retryOfQueueItemId: null,
      completedAttemptId: 60,
      legacyTestQuestionId: 5,
      introducedLocalDate: "2026-07-22",
    },
    {
      id: 41,
      sessionId: 30,
      cardId: 21,
      position: 2,
      status: "skipped",
      retryOfQueueItemId: 40,
      completedAttemptId: null,
      legacyTestQuestionId: null,
      introducedLocalDate: null,
    },
  ],
  studyPrompts: [{
    id: 50,
    sessionId: 30,
    queueItemId: 40,
    promptKey: "review:30:40",
    promptText: "What does Straße mean?",
    status: "completed",
    preparedAt: "2026-07-22T16:00:00Z",
    deliveredAt: "2026-07-22T16:00:05Z",
    answeredAt: "2026-07-22T16:30:00Z",
  }],
  deliveryAttempts: [
    {
      id: 55,
      promptId: 50,
      attemptNumber: 1,
      status: "failed",
      attemptedAt: "2026-07-22T16:00:01Z",
      receiptAt: null,
      outboundDeliveryId: null,
      contentFingerprint: "b".repeat(64),
      errorText: "429 Too Many Requests",
    },
    {
      id: 56,
      promptId: 50,
      attemptNumber: 2,
      status: "delivered",
      attemptedAt: "2026-07-22T16:00:04Z",
      receiptAt: "2026-07-22T16:00:05Z",
      outboundDeliveryId: "telegram:9182",
      contentFingerprint: "b".repeat(64),
      errorText: null,
    },
  ],
  answerDrafts: [{
    id: 58,
    promptId: 50,
    submittedAnswer: "a street",
    evaluatorGrade: "partial",
    evaluationFeedback: "close enough",
    answeredAt: "2026-07-22T16:30:00Z",
    createdAt: "2026-07-22T16:30:00Z",
  }],
  reviewAttempts: [
    {
      id: 60,
      cardId: 20,
      sessionId: 30,
      queueItemId: 40,
      promptId: 50,
      answerDraftId: 58,
      source: "review",
      rating: "hard",
      submittedAnswer: "a street",
      evaluatorGrade: "partial",
      evaluationFeedback: "close enough",
      reviewedAt: "2026-07-22T16:30:00Z",
      beforeState: "new",
      beforeStability: null,
      beforeDifficulty: null,
      beforeDueAt: "2026-07-20T00:00:00Z",
      beforeEffectiveDueAt: "2026-07-20T00:00:00Z",
      beforeLastReviewAt: null,
      beforeRepetitions: 0,
      beforeLapses: 0,
      afterState: "review",
      afterStability: "3.2173",
      afterDifficulty: "5.0",
      afterRawDueAt: "2026-07-25T16:30:00Z",
      afterEffectiveDueAt: "2026-07-26T00:00:00Z",
      afterLastReviewAt: "2026-07-22T16:30:00Z",
      afterRepetitions: 1,
      afterLapses: 0,
      schedulerKind: "fsrs",
      schedulerVersion: "6",
      parametersVersion: "fsrs-6-default",
      parameterFingerprint: "a".repeat(64),
      desiredRetention: "0.9",
      isSameSessionRetry: 0,
      retryOfAttemptId: null,
      legacySource: null,
      legacyId: null,
      createdAt: "2026-07-22T16:30:00Z",
    },
    {
      id: 61,
      cardId: 21,
      sessionId: null,
      queueItemId: null,
      promptId: null,
      answerDraftId: null,
      source: "migration",
      rating: "again",
      submittedAnswer: null,
      evaluatorGrade: null,
      evaluationFeedback: null,
      reviewedAt: "2026-07-19T12:00:00Z",
      beforeState: "new",
      beforeStability: null,
      beforeDifficulty: null,
      beforeDueAt: "2026-07-19T12:00:00Z",
      beforeEffectiveDueAt: "2026-07-19T12:00:00Z",
      beforeLastReviewAt: null,
      beforeRepetitions: 0,
      beforeLapses: 0,
      afterState: "relearning",
      afterStability: "0.2172",
      afterDifficulty: "10.0",
      afterRawDueAt: "2026-07-19T12:10:00Z",
      afterEffectiveDueAt: "2026-07-20T00:00:00Z",
      afterLastReviewAt: "2026-07-19T12:00:00Z",
      afterRepetitions: 1,
      afterLapses: 1,
      schedulerKind: "fsrs",
      schedulerVersion: "6",
      parametersVersion: "fsrs-6-default",
      parameterFingerprint: "a".repeat(64),
      desiredRetention: "0.9",
      isSameSessionRetry: 1,
      retryOfAttemptId: 60,
      legacySource: "review_events",
      legacyId: 3,
      createdAt: "2026-07-19T12:00:00Z",
    },
  ],
} as const satisfies SnapshotV2;

function stub() {
  return env.VOCABULARY.getByName(`snapshot-${crypto.randomUUID()}`);
}

/** Deeply mutable mirror of SnapshotV2 so corruption edits stay type-checked. */
type Draft = {
  -readonly [K in keyof SnapshotV2]: SnapshotV2[K] extends readonly (infer Row)[]
    ? { -readonly [F in keyof Row]: Row[F] }[]
    : SnapshotV2[K];
};

function mutate(change: (draft: Draft) => void): unknown {
  // JSON.parse returns `any`; the clone is structurally SnapshotV2 by construction.
  const draft = JSON.parse(JSON.stringify(SNAPSHOT)) as Draft;
  change(draft);
  return draft;
}

describe("SnapshotV2 and JCS", () => {
  it("matches the cross-language constrained JCS vector", async () => {
    const vector = {
      nested: { z: null, a: true },
      id: 9_007_199_254_740_991,
      array: ["café", "Straße", "😀", "\u2028", "\"", "\\", "\u0000\b\t\n\f\r\u001f"],
    };
    expect(canonicalizeJcs(vector)).toBe(
      "{\"array\":[\"café\",\"Straße\",\"😀\",\"\u2028\",\"\\\"\",\"\\\\\",\"\\u0000\\b\\t\\n\\f\\r\\u001f\"],\"id\":9007199254740991,\"nested\":{\"a\":true,\"z\":null}}",
    );
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(canonicalizeJcs(vector)),
    );
    expect(Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""))
      .toBe("f9aff60e240798e6b07e32bcfd2d38464a36e899642f167589c4a660752a61f6");
    expect(() => canonicalizeJcs("\ud800")).toThrow(/lone surrogate/u);
  });

  it("renders FSRS scalars the way Python's repr does", () => {
    expect(encodeReal(1)).toBe("1.0");
    expect(encodeReal(10)).toBe("10.0");
    expect(encodeReal(0.9)).toBe("0.9");
    expect(encodeReal(3.2173)).toBe("3.2173");
    expect(encodeReal(0.0001)).toBe("0.0001");
    // Outside the band both runtimes render as plain decimals.
    expect(() => encodeReal(1e-5)).toThrow(/outside the snapshot domain/u);
    expect(() => encodeReal(1e16)).toThrow(/outside the snapshot domain/u);
    expect(() => encodeReal(-1)).toThrow(/outside the snapshot domain/u);
    expect(() => encodeReal(Number.NaN)).toThrow(/outside the snapshot domain/u);
  });

  it("round-trips every v5 table through Durable Object SQLite unchanged", async () => {
    expect(parseSnapshot(SNAPSHOT)).toEqual(SNAPSHOT);
    await runInDurableObject(stub(), (_instance, state) => {
      initializeSchema(state.storage.sql);
      writeSnapshot(state.storage, SNAPSHOT);
      Array.from(state.storage.sql.exec(
        `INSERT INTO inbox_events
          (dedupe_key, kind, status, payload, prepared_target_id, normalized_key,
           coalesced_to_event_id, response_text, created_at, updated_at)
         VALUES (?, 'telegram', 'ready', NULL, NULL, ?, NULL, ?, ?, ?)`,
        "telegram:legacy-explicit-columns",
        "aster",
        "✓ Saved.",
        "2026-08-27T00:00:00Z",
        "2026-08-27T00:00:00Z",
      ));
      expect(Array.from(state.storage.sql.exec<{ visual_intent: string | null }>(
        "SELECT visual_intent FROM inbox_events",
      ))).toEqual([{ visual_intent: null }]);
      Array.from(state.storage.sql.exec(
        "UPDATE inbox_events SET visual_intent = ?",
        JSON.stringify({
          senseIndex: 0,
          category: "plant",
          query: "aster flower",
          description: "An aster flower.",
        }),
      ));
      const exported = readSnapshot(state.storage);
      expect(exported).toEqual(SNAPSHOT);
      expect(canonicalizeJcs(exported)).toBe(canonicalizeJcs(SNAPSHOT));
      expect(() => writeSnapshot(state.storage, SNAPSHOT)).toThrow(/empty storage/u);
    });
  });

  it("summarises every table and verifies the envelope digest", async () => {
    const sha256 = await sha256Snapshot(SNAPSHOT);
    // tests/unit/test_cloudflare_snapshot.py asserts the same digest over a
    // byte-identical fixture: proof the two implementations agree on the wire.
    expect(sha256).toBe("d4fa50222abf362c042a16b3402878990f994480f1c59f7128e88a91fbc4bba8");
    expect(summarizeSnapshot(SNAPSHOT, sha256)).toEqual({
      entries: 1,
      senses: 2,
      reviewEvents: 1,
      testSessions: 1,
      testQuestions: 1,
      cards: 2,
      studySessions: 1,
      studyQueue: 2,
      studyPrompts: 1,
      deliveryAttempts: 2,
      answerDrafts: 1,
      reviewAttempts: 2,
      sha256,
    });
    await expect(parseVerifiedEnvelope({ sha256, snapshot: SNAPSHOT })).resolves.toEqual({
      sha256,
      snapshot: SNAPSHOT,
    });
    const tampered = mutate((draft) => {
      draft.entries[0].displayText = "Strasse";
    });
    await expect(parseVerifiedEnvelope({ sha256, snapshot: tampered })).resolves.toBeNull();
    await expect(parseVerifiedEnvelope({ sha256: "0".repeat(64), snapshot: SNAPSHOT }))
      .resolves.toBeNull();
  });

  it("refuses v4 snapshots, non-canonical reals, and dangling references", () => {
    expect(parseSnapshot({ ...SNAPSHOT, formatVersion: 1 })).toBeNull();
    expect(parseSnapshot({
      formatVersion: 1,
      entries: SNAPSHOT.entries,
      senses: SNAPSHOT.senses,
      reviewEvents: SNAPSHOT.reviewEvents,
      testSessions: SNAPSHOT.testSessions,
      testQuestions: SNAPSHOT.testQuestions,
    })).toBeNull();
    for (const value of ["1", "1.00", "0.90", "3.2173e0", "01.0", ""]) {
      expect(parseSnapshot(mutate((draft) => {
        draft.cards[0].difficulty = value;
      }))).toBeNull();
    }
    expect(parseSnapshot(mutate((draft) => {
      draft.cards[1].senseId = 99;
    }))).toBeNull();
    expect(parseSnapshot(mutate((draft) => {
      draft.studyQueue[0].completedAttemptId = null;
    }))).toBeNull();
    expect(parseSnapshot(mutate((draft) => {
      draft.reviewAttempts[0].afterRepetitions = 3;
    }))).toBeNull();
    expect(parseSnapshot(mutate((draft) => {
      draft.studyPrompts[0].status = "prepared";
    }))).toBeNull();
    expect(parseSnapshot(mutate((draft) => {
      draft.cards[0].state = "new";
    }))).toBeNull();
  });

  it("accepts pre-grading legacy review events but still binds the answer to the status", () => {
    // v4 recorded answers before it recorded grades; those rows are real
    // history and must cross the bridge rather than block the export.
    const ungraded = parseSnapshot(mutate((draft) => {
      draft.reviewEvents[0].grade = null;
      draft.reviewEvents[0].evaluationFeedback = null;
    }));
    expect(ungraded).not.toBeNull();
    expect(ungraded!.reviewEvents[0]!.grade).toBeNull();

    expect(parseSnapshot(mutate((draft) => {
      draft.reviewEvents[0].answerText = null;
    }))).toBeNull();
    expect(parseSnapshot(mutate((draft) => {
      draft.reviewEvents[0].answeredAt = null;
    }))).toBeNull();
    expect(parseSnapshot(mutate((draft) => {
      draft.reviewEvents[0].status = "missed";
    }))).toBeNull();
  });
});

describe("v5 schema", () => {
  it("keeps the append-only triggers and drops the v4-only indexes", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      const sql = state.storage.sql;
      initializeSchema(sql);
      writeSnapshot(state.storage, SNAPSHOT);

      const names = (type: string) =>
        Array.from(sql.exec<{ name: string }>(
          "SELECT name FROM sqlite_master WHERE type = ? ORDER BY name",
          type,
        )).map(({ name }) => name);

      expect(names("table")).toEqual(expect.arrayContaining([
        "answer_drafts", "companion_state", "inbox_events", "prompt_delivery_attempts",
        "review_attempts", "review_events", "study_prompts", "study_queue",
        "study_sessions", "test_questions", "test_sessions", "vocabulary_cards",
        "vocabulary_entries", "vocabulary_senses",
      ]));
      expect(names("trigger")).toEqual([
        "answer_drafts_immutable_delete",
        "answer_drafts_immutable_update",
        "prompt_delivery_attempts_immutable_delete",
        "prompt_delivery_attempts_immutable_update",
        "review_attempts_immutable_delete",
        "review_attempts_immutable_update",
        "study_prompt_content_immutable",
        "study_prompt_status_monotonic",
        "study_prompt_terminal_status_immutable",
      ]);
      const indexes = names("index");
      expect(indexes).toEqual(expect.arrayContaining([
        "one_active_study_prompt_idx", "one_current_queue_item_idx",
        "one_final_attempt_per_prompt_idx", "one_forward_card_per_entry_idx",
        "one_migration_attempt_per_legacy_row_idx", "one_open_study_session_idx",
        "one_prompt_per_queue_occurrence_idx", "one_retry_occurrence_idx",
        "one_reverse_card_per_sense_idx", "review_attempts_card_time_idx",
        "study_queue_order_idx", "vocabulary_cards_due_idx", "vocabulary_cards_entry_idx",
        "vocabulary_senses_entry_order_idx", "vocabulary_senses_id_entry_idx",
      ]));
      for (const dropped of [
        "one_active_test_session_idx", "review_events_entry_id_idx",
        "test_questions_session_position_idx", "vocabulary_review_order_idx",
      ]) {
        expect(indexes).not.toContain(dropped);
      }

      // A live prompt on queue item 41: only its own status may move forward.
      Array.from(sql.exec(
        `INSERT INTO study_prompts
           (id, session_id, queue_item_id, prompt_key, prompt_text, status, prepared_at, delivered_at, answered_at)
         VALUES (51, 30, 41, 'review:30:41', 'Second prompt?', 'delivered', ?, ?, NULL)`,
        "2026-07-22T16:40:00Z",
        "2026-07-22T16:40:05Z",
      ));
      expect(() => Array.from(sql.exec(
        "UPDATE study_prompts SET status = 'prepared' WHERE id = 51",
      ))).toThrow(/cannot regress/u);

      // Prompt 50 is 'completed': its terminal status and content are frozen.
      expect(() => Array.from(sql.exec(
        "UPDATE study_prompts SET status = 'failed' WHERE id = 50",
      ))).toThrow(/terminal status is immutable/u);
      expect(() => Array.from(sql.exec(
        "UPDATE study_prompts SET prompt_text = 'rewritten' WHERE id = 50",
      ))).toThrow(/content is immutable/u);

      for (const statement of [
        "UPDATE prompt_delivery_attempts SET status = 'unknown' WHERE id = 55",
        "DELETE FROM prompt_delivery_attempts WHERE id = 55",
        "UPDATE answer_drafts SET submitted_answer = 'rewritten' WHERE id = 58",
        "DELETE FROM answer_drafts WHERE id = 58",
        "UPDATE review_attempts SET rating = 'easy' WHERE id = 60",
        "DELETE FROM review_attempts WHERE id = 60",
      ]) {
        expect(() => Array.from(sql.exec(statement))).toThrow(/are immutable/u);
      }

      // One forward card per entry, one reverse card per sense.
      const clone = (newId: number, sourceId: number) =>
        `INSERT INTO vocabulary_cards SELECT ${newId}, entry_id, sense_id, direction, state,
           stability, difficulty, due_at, effective_due_at, last_review_at, repetitions, lapses,
           scheduler_kind, scheduler_version, parameters_version, parameter_fingerprint,
           desired_retention, introduced_local_date, buried_until_local_date, created_at
         FROM vocabulary_cards WHERE id = ${sourceId}`;
      expect(() => Array.from(sql.exec(clone(22, 20))))
        .toThrow(/UNIQUE constraint failed: vocabulary_cards\.entry_id/u);
      expect(() => Array.from(sql.exec(clone(23, 21))))
        .toThrow(/UNIQUE constraint failed: vocabulary_cards\.sense_id/u);
    });
  });
});
