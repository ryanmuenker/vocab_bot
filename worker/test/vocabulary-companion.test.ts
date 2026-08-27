import { env } from "cloudflare:workers";
import { evictDurableObject, runDurableObjectAlarm, runInDurableObject } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DESIRED_RETENTION,
  PARAMETERS_VERSION,
  PARAMETER_FINGERPRINT,
  SCHEDULER_KIND,
  SCHEDULER_VERSION,
} from "../src/domain/scheduling";
import { encodeReal } from "../src/domain/snapshot";
import { normalizeReverseAnswer } from "../src/domain/routing";
import type { SnapshotCard, SnapshotV2 } from "../src/domain/snapshot";

const VISUAL_INTENT = {
  senseIndex: 0,
  category: "object",
  query: "street object",
  description: "An object beside a paved street.",
} as const;

const PROVIDER_VISUAL = {
  sense_index: 0,
  category: "object",
  query: "street object",
  description: "An object beside a paved street.",
} as const;

function message(updateId: number, text: string, receivedAt = "2026-07-23T04:00:00Z") {
  return {
    updateId,
    messageId: updateId,
    chatId: "123456",
    senderId: "123456",
    text,
    receivedAt,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function newCard(
  id: number,
  entryId: number,
  senseId: number | null,
  direction: "forward" | "reverse",
  createdAt: string,
): SnapshotCard {
  return {
    id,
    entryId,
    senseId,
    direction,
    state: "new",
    stability: null,
    difficulty: null,
    dueAt: createdAt,
    effectiveDueAt: createdAt,
    lastReviewAt: null,
    repetitions: 0,
    lapses: 0,
    schedulerKind: SCHEDULER_KIND,
    schedulerVersion: SCHEDULER_VERSION,
    parametersVersion: PARAMETERS_VERSION,
    parameterFingerprint: PARAMETER_FINGERPRINT,
    desiredRetention: encodeReal(DESIRED_RETENTION),
    introducedLocalDate: null,
    buriedUntilLocalDate: null,
    createdAt,
  };
}

/** A library of `count` single-sense entries, each with both card directions. */
function library(count: number): SnapshotV2 {
  const entries: SnapshotV2["entries"][number][] = [];
  const senses: SnapshotV2["senses"][number][] = [];
  const cards: SnapshotCard[] = [];
  for (let index = 1; index <= count; index += 1) {
    const dateAdded = `2026-06-${String(index).padStart(2, "0")}T00:00:00Z`;
    entries.push({
      id: index,
      displayText: `word-${index}`,
      normalizedText: `word-${index}`,
      dateAdded,
      lastReviewed: null,
      reviewStatus: "new",
    });
    senses.push({
      id: index,
      entryId: index,
      definition: `definition ${index}`,
      partOfSpeech: "noun",
      exampleSentence: `Example ${index}.`,
      sourceContext: null,
      dateAdded,
    });
    cards.push(newCard(index * 2 - 1, index, null, "forward", dateAdded));
    cards.push(newCard(index * 2, index, index, "reverse", dateAdded));
  }
  return {
    formatVersion: 2,
    entries,
    senses,
    reviewEvents: [],
    testSessions: [],
    testQuestions: [],
    cards,
    studySessions: [],
    studyQueue: [],
    studyPrompts: [],
    deliveryAttempts: [],
    answerDrafts: [],
    reviewAttempts: [],
  };
}

interface Transport {
  readonly sent: string[];
  readonly modelCalls: () => number;
  telegramFails: boolean;
  modelStatus: number;
  evaluation: { grade: string; feedback: string };
  definitionVisual: unknown;
}

/** Stub the model and Telegram endpoints, recording what each one saw. */
function transport(): Transport {
  const sent: string[] = [];
  let modelCalls = 0;
  let messageId = 1_000;
  const state: Transport = {
    sent,
    modelCalls: () => modelCalls,
    telegramFails: false,
    modelStatus: 200,
    evaluation: { grade: "correct", feedback: "Accurate." },
    definitionVisual: undefined,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/responses") || url.includes("/chat/completions")) {
        modelCalls += 1;
        if (state.modelStatus !== 200) return jsonResponse({}, state.modelStatus);
        const body = JSON.parse(init!.body as string) as {
          instructions?: string;
          messages?: { content: string }[];
        };
        const systemPrompt = body.instructions ?? body.messages?.[0]?.content ?? "";
        const content = systemPrompt.includes("dictionary")
          ? JSON.stringify({
              senses: [
                {
                  part_of_speech: "noun",
                  definition: state.definitionVisual === undefined
                    ? "a street"
                    : "an object beside a street",
                  example_sentence: state.definitionVisual === undefined
                    ? "The street was quiet."
                    : "The object stood beside the street.",
                },
              ],
              ...(state.definitionVisual === undefined
                ? {}
                : { visual: state.definitionVisual }),
            })
          : JSON.stringify(state.evaluation);
        return url.includes("/responses")
          ? jsonResponse({
              output: [{
                type: "message",
                content: [{ type: "output_text", text: content }],
              }],
            })
          : jsonResponse({ choices: [{ message: { content } }] });
      }
      if (state.telegramFails) return jsonResponse({ ok: false });
      sent.push(JSON.parse(init!.body as string).text as string);
      messageId += 1;
      return jsonResponse({ ok: true, result: { message_id: messageId } });
    }),
  );
  return state;
}

async function drain(stub: DurableObjectStub, limit = 12): Promise<void> {
  for (let attempt = 0; attempt < limit; attempt += 1) {
    if (!(await runDurableObjectAlarm(stub))) return;
  }
}

function companion(name: string) {
  return env.VOCABULARY.getByName(`${name}-${crypto.randomUUID()}`);
}

afterEach(() => vi.unstubAllGlobals());

describe("VocabularyCompanion capture", () => {
  it("deduplicates updates, coalesces equivalent captures, and projects both card directions", async () => {
    const io = transport();
    const stub = companion("coalesce");

    expect(await stub.enqueueTelegramUpdate(message(1, "Straße"))).toBe("enqueued");
    expect(await stub.enqueueTelegramUpdate(message(1, "Straße"))).toBe("duplicate");
    expect(await stub.enqueueTelegramUpdate(message(2, "  STRASSE  "))).toBe("enqueued");
    await drain(stub);

    expect(io.modelCalls()).toBe(1);
    expect(io.sent).toHaveLength(2);
    const exported = (await stub.exportSnapshot())!;
    expect(exported.entries).toHaveLength(1);
    expect(exported.cards.map((card) => [card.direction, card.senseId])).toEqual([
      ["forward", null],
      ["reverse", exported.senses[0]!.id],
    ]);
    expect(await stub.summary()).toMatchObject({ entries: 1, senses: 1, pendingInbox: 0, failedInbox: 0 });
  });

  it("keeps one leader intent through retries and eviction while every duplicate stays null", async () => {
    const io = transport();
    io.definitionVisual = PROVIDER_VISUAL;
    io.telegramFails = true;
    const stub = companion("visual-intent-lifecycle");

    await stub.enqueueTelegramUpdate(message(1, "Street"));
    await stub.enqueueTelegramUpdate(message(2, " street "));
    expect(await runInDurableObject(stub, (_instance, state) =>
      Array.from(state.storage.sql.exec<{
        dedupe_key: string;
        status: string;
        visual_intent: string | null;
      }>(
        `SELECT dedupe_key, status, visual_intent
         FROM inbox_events ORDER BY id`,
      ))
    )).toEqual([
      { dedupe_key: "telegram:1", status: "pending", visual_intent: null },
      { dedupe_key: "telegram:2", status: "waiting", visual_intent: null },
    ]);

    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await runInDurableObject(stub, (_instance, state) =>
      Array.from(state.storage.sql.exec<{
        dedupe_key: string;
        status: string;
        response_text: string | null;
        visual_intent: string | null;
      }>(
        `SELECT dedupe_key, status, response_text, visual_intent
         FROM inbox_events ORDER BY id`,
      ))
    )).toEqual([
      expect.objectContaining({
        dedupe_key: "telegram:1",
        status: "ready",
        response_text: expect.stringContaining("✓ Saved."),
        visual_intent: JSON.stringify(VISUAL_INTENT),
      }),
      expect.objectContaining({
        dedupe_key: "telegram:2",
        status: "ready",
        response_text: expect.stringContaining("✓ Saved."),
        visual_intent: null,
      }),
    ]);

    await evictDurableObject(stub);
    expect(await runInDurableObject(stub, (_instance, state) =>
      Array.from(state.storage.sql.exec<{ visual_intent: string | null }>(
        "SELECT visual_intent FROM inbox_events ORDER BY id",
      ))
    )).toEqual([
      { visual_intent: JSON.stringify(VISUAL_INTENT) },
      { visual_intent: null },
    ]);

    io.telegramFails = false;
    await drain(stub);
    expect(await stub.enqueueTelegramUpdate(message(3, "STREET"))).toBe("enqueued");
    expect(await runInDurableObject(stub, (_instance, state) =>
      Array.from(state.storage.sql.exec<{
        status: string;
        response_text: string | null;
        visual_intent: string | null;
      }>(
        `SELECT status, response_text, visual_intent
         FROM inbox_events WHERE dedupe_key = 'telegram:3'`,
      ))
    )).toEqual([{
      status: "ready",
      response_text: expect.stringContaining("Already saved."),
      visual_intent: null,
    }]);
    await drain(stub);
    expect(await runInDurableObject(stub, (_instance, state) =>
      Array.from(state.storage.sql.exec<{ status: string; visual_intent: string | null }>(
        "SELECT status, visual_intent FROM inbox_events ORDER BY id",
      ))
    )).toEqual([
      { status: "completed", visual_intent: null },
      { status: "completed", visual_intent: null },
      { status: "completed", visual_intent: null },
    ]);
  });

  it("copies a ready capture response to a follower without copying its intent", async () => {
    transport();
    const stub = companion("visual-ready-follower");
    await stub.enqueueTelegramUpdate(message(1, "Street"));
    await runInDurableObject(stub, (_instance, state) => {
      Array.from(state.storage.sql.exec(
        `UPDATE inbox_events
         SET status = 'ready', payload = NULL, response_text = ?,
             visual_intent = ?
         WHERE dedupe_key = 'telegram:1'`,
        "prepared definition",
        JSON.stringify(VISUAL_INTENT),
      ));
    });

    await stub.enqueueTelegramUpdate(message(2, " street "));

    expect(await runInDurableObject(stub, (_instance, state) =>
      Array.from(state.storage.sql.exec<{
        dedupe_key: string;
        response_text: string | null;
        visual_intent: string | null;
      }>(
        `SELECT dedupe_key, response_text, visual_intent
         FROM inbox_events ORDER BY id`,
      ))
    )).toEqual([
      {
        dedupe_key: "telegram:1",
        response_text: "prepared definition",
        visual_intent: JSON.stringify(VISUAL_INTENT),
      },
      {
        dedupe_key: "telegram:2",
        response_text: "prepared definition",
        visual_intent: null,
      },
    ]);
    await runInDurableObject(stub, async (_instance, state) => {
      Array.from(state.storage.sql.exec(
        `UPDATE inbox_events
         SET status = 'completed', response_text = NULL, visual_intent = NULL
         WHERE status = 'ready'`,
      ));
      await state.storage.deleteAlarm();
    });
  });

  it("clears intent on generic and terminal delivery failures", async () => {
    const io = transport();
    io.definitionVisual = PROVIDER_VISUAL;
    io.telegramFails = true;
    const stub = companion("visual-intent-failures");
    await stub.enqueueTelegramUpdate(message(1, "Street"));
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    await runInDurableObject(stub, (_instance, state) => {
      Array.from(state.storage.sql.exec(
        "UPDATE inbox_events SET attempt_count = 9 WHERE dedupe_key = 'telegram:1'",
      ));
    });
    expect(await runDurableObjectAlarm(stub)).toBe(true);

    await stub.enqueueTelegramUpdate(message(2, "Another street"));
    await runInDurableObject(stub, (_instance, state) => {
      Array.from(state.storage.sql.exec(
        `UPDATE inbox_events
         SET payload = '{', visual_intent = ?
         WHERE dedupe_key = 'telegram:2'`,
        JSON.stringify(VISUAL_INTENT),
      ));
    });
    expect(await runDurableObjectAlarm(stub)).toBe(true);

    expect(await runInDurableObject(stub, (_instance, state) =>
      Array.from(state.storage.sql.exec<{
        dedupe_key: string;
        status: string;
        visual_intent: string | null;
      }>(
        `SELECT dedupe_key, status, visual_intent
         FROM inbox_events ORDER BY id`,
      ))
    )).toEqual([
      { dedupe_key: "telegram:1", status: "failed", visual_intent: null },
      { dedupe_key: "telegram:2", status: "failed", visual_intent: null },
    ]);
  });

  it("keeps imported senses while pruning untouched reverse cards to three diverse meanings", async () => {
    const stub = companion("reverse-cap-import");
    const snapshot = library(1);
    const dateAdded = snapshot.entries[0]!.dateAdded;
    const definitions = [
      ["noun", "a place that stores money for customers", "She deposited her pay at the bank."],
      ["noun", "a business that stores money for customers", "The bank approved the loan."],
      ["noun", "the sloping land beside a river", "They picnicked on the river bank."],
      ["verb", "to tilt an aircraft during a turn", "The pilot banked left."],
      ["noun", "an organization that stores money for customers", "The bank safeguards deposits."],
    ] as const;
    const senses: SnapshotV2["senses"] = definitions.map(
      ([partOfSpeech, definition, exampleSentence], index) => ({
        id: index + 1,
        entryId: 1,
        definition,
        partOfSpeech,
        exampleSentence,
        sourceContext: null,
        dateAdded,
      }),
    );
    const cards = [
      newCard(1, 1, null, "forward", dateAdded),
      ...senses.map((sense, index) => newCard(index + 2, 1, sense.id, "reverse", dateAdded)),
    ];

    const summary = await stub.importSnapshot({ ...snapshot, senses, cards });
    const exported = (await stub.exportSnapshot())!;

    expect(summary).toMatchObject({ entries: 1, senses: 5, cards: 4 });
    expect(exported.senses).toHaveLength(5);
    expect(
      exported.cards
        .filter((card) => card.direction === "reverse")
        .map((card) => card.senseId),
    ).toEqual([1, 3, 4]);
  });

  it("prunes existing untouched reverse cards when the Durable Object restarts", async () => {
    const stub = companion("reverse-cap-restart");
    const snapshot = library(1);
    const dateAdded = snapshot.entries[0]!.dateAdded;
    await stub.importSnapshot(snapshot);
    await runInDurableObject(stub, (_instance, state) => {
      for (let senseId = 2; senseId <= 5; senseId += 1) {
        Array.from(
          state.storage.sql.exec(
            `INSERT INTO vocabulary_senses
               (id, entry_id, definition, part_of_speech, example_sentence,
                source_context, date_added)
             VALUES (?, 1, ?, 'noun', ?, NULL, ?)`,
            senseId,
            `legacy definition ${senseId}`,
            `Legacy example ${senseId}.`,
            dateAdded,
          ),
        );
        Array.from(
          state.storage.sql.exec(
            `INSERT INTO vocabulary_cards (
               id, entry_id, sense_id, direction, state, stability, difficulty,
               due_at, effective_due_at, last_review_at, repetitions, lapses,
               scheduler_kind, scheduler_version, parameters_version,
               parameter_fingerprint, desired_retention,
               introduced_local_date, buried_until_local_date, created_at
             )
             SELECT ?, entry_id, ?, 'reverse', state, stability, difficulty,
                    due_at, effective_due_at, last_review_at, repetitions, lapses,
                    scheduler_kind, scheduler_version, parameters_version,
                    parameter_fingerprint, desired_retention,
                    introduced_local_date, buried_until_local_date, created_at
             FROM vocabulary_cards
             WHERE id = 1`,
            senseId + 1,
            senseId,
          ),
        );
      }
      Array.from(state.storage.sql.exec("DELETE FROM maintenance_migrations"));
      expect(
        state.storage.sql.exec<{ count: number }>(
          "SELECT COUNT(*) AS count FROM vocabulary_cards WHERE direction = 'reverse'",
        ).one().count,
      ).toBe(5);
    });

    await evictDurableObject(stub);
    const exported = (await stub.exportSnapshot())!;

    expect(exported.senses).toHaveLength(5);
    expect(exported.cards.filter((card) => card.direction === "reverse")).toHaveLength(3);
  });
});

describe("VocabularyCompanion automatic review pause", () => {
  it("exits an active review, captures words while paused, and resumes explicitly", async () => {
    const io = transport();
    const stub = companion("pause-active-review");
    await stub.importSnapshot(library(2));

    await stub.enqueueTelegramUpdate(message(1, "/review", "2026-07-23T04:00:00Z"));
    await drain(stub);
    expect(io.sent[0]).toContain("What does 'word-1' mean?");

    await stub.enqueueTelegramUpdate(message(2, "/pause", "2026-07-23T04:01:00Z"));
    await drain(stub);
    expect(io.sent[1]).toBe(
      "Automatic reviews paused. The pause ends 10 minutes after your last vocabulary request. " +
        "Use /unpause to resume sooner.",
    );

    await stub.enqueueTelegramUpdate(message(3, "obdurate", "2026-07-23T04:02:00Z"));
    await drain(stub);
    expect(io.modelCalls()).toBe(1);
    expect(io.sent[2]).toContain("obdurate");
    expect((await stub.summary()).entries).toBe(3);

    await stub.enqueueTelegramUpdate(message(4, "/unpause", "2026-07-23T04:03:00Z"));
    await drain(stub);
    expect(io.sent[3]).toBe("Automatic reviews resumed.");

    expect(await stub.enqueueDailyReview({
      dedupeKey: "pause-manual-resume",
      nowUtc: "2026-07-23T04:04:00Z",
    })).toBe("enqueued");
    await drain(stub);
    expect(io.sent[4]).toContain("What does 'word-1' mean?");
  });

  it("persists across eviction, extends on vocabulary requests, and expires after inactivity", async () => {
    const io = transport();
    const stub = companion("pause-inactivity");
    await stub.importSnapshot(library(1));

    await stub.enqueueTelegramUpdate(message(1, "/pause", "2026-07-23T04:00:00Z"));
    await drain(stub);
    await evictDurableObject(stub);

    expect(await stub.enqueueDailyReview({
      dedupeKey: "pause-before-word",
      nowUtc: "2026-07-23T04:09:00Z",
    })).toBe("silent");
    await stub.enqueueTelegramUpdate(message(2, "obdurate", "2026-07-23T04:09:00Z"));
    await drain(stub);
    await evictDurableObject(stub);

    expect(await stub.enqueueDailyReview({
      dedupeKey: "pause-after-word",
      nowUtc: "2026-07-23T04:18:00Z",
    })).toBe("silent");
    expect(await stub.enqueueDailyReview({
      dedupeKey: "pause-expired",
      nowUtc: "2026-07-23T04:20:00Z",
    })).toBe("enqueued");
    await drain(stub);

    expect(io.sent).toHaveLength(3);
    expect(io.sent[1]).toContain("obdurate");
    expect(io.sent[2]).toContain("What does 'word-1' mean?");
  });

  it("allows an explicit review while automatic prompts remain paused", async () => {
    const io = transport();
    const stub = companion("pause-manual-review");
    await stub.importSnapshot(library(1));

    await stub.enqueueTelegramUpdate(message(1, "/pause", "2026-07-23T04:00:00Z"));
    await drain(stub);
    await stub.enqueueTelegramUpdate(message(2, "/review", "2026-07-23T04:01:00Z"));
    await drain(stub);

    expect(io.sent[1]).toContain("What does 'word-1' mean?");
  });
});

describe("VocabularyCompanion delivery gating", () => {
  it("never makes a prompt answerable when its send fails, and echoes the next message", async () => {
    const io = transport();
    io.telegramFails = true;
    const stub = companion("failed-send");
    await stub.importSnapshot(library(2));

    expect(await stub.enqueueTelegramUpdate(message(1, "/review"))).toBe("enqueued");
    await drain(stub, 12);
    expect(await stub.summary()).toMatchObject({ pendingInbox: 0, failedInbox: 1 });
    expect(io.sent).toHaveLength(0);

    // The prompt was never delivered, so the next ordinary message is neither an
    // answer nor a capture: it comes back verbatim with the outstanding question.
    io.telegramFails = false;
    expect(await stub.enqueueTelegramUpdate(message(2, "obdurate"))).toBe("enqueued");
    await drain(stub);

    expect(io.modelCalls()).toBe(0);
    expect(io.sent).toHaveLength(1);
    expect(io.sent[0]).toContain("Review due. Answer this delivered question first:");
    expect(io.sent[0]).toContain("What does 'word-1' mean?");
    expect(io.sent[0]).toContain("Your original message was:\nobdurate");
    expect(io.sent[0]).toContain("Complete or exit the study session, then resubmit it.");
    expect((await stub.summary()).entries).toBe(2);
  });

  it("still interrupts capture after a day rollover cancels the undelivered prompt", async () => {
    const io = transport();
    io.telegramFails = true;
    const stub = companion("rollover-gap");
    await stub.importSnapshot(library(2));

    expect(await stub.enqueueTelegramUpdate(message(1, "/review"))).toBe("enqueued");
    await drain(stub, 12);
    expect(io.sent).toHaveLength(0);

    // The next ordinary message arrives on the following local day: the
    // rollover reconcile cancels the prepared prompt, but the message must
    // still surface the review instead of being captured.
    io.telegramFails = false;
    expect(
      await stub.enqueueTelegramUpdate(message(2, "obdurate", "2026-07-24T04:00:00Z")),
    ).toBe("enqueued");
    await drain(stub);

    expect(io.modelCalls()).toBe(0);
    expect(io.sent).toHaveLength(1);
    expect(io.sent[0]).toContain("Review due. Answer this delivered question first:");
    expect(io.sent[0]).toContain("Your original message was:\nobdurate");
    expect((await stub.summary()).entries).toBe(2);
  });

  it("replays undelivered evaluation feedback before accepting a rating", async () => {
    const io = transport();
    const stub = companion("evaluation-delivery");
    await stub.importSnapshot(library(2));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    await drain(stub);

    io.telegramFails = true;
    await stub.enqueueTelegramUpdate(message(2, "a street of some kind"));
    for (let attempt = 0; attempt < 10; attempt += 1) {
      expect(await runDurableObjectAlarm(stub)).toBe(true);
    }

    io.telegramFails = false;
    await stub.enqueueTelegramUpdate(message(3, "not a rating"));
    await drain(stub);

    expect(io.sent[1]).toContain("Your previous evaluation was not delivered");
    expect(io.sent[1]).toContain("Grade: Correct");
    expect(io.sent[1]).toContain("Choose effort: Hard or Good or Easy.");
    expect(io.sent[1]).toContain("Your original message was:\nnot a rating");

    await stub.enqueueTelegramUpdate(message(4, "good"));
    await drain(stub);
    expect(io.sent[2]).toContain("Rated: Good");
  });

  it("does not send a prompt that was cancelled before delivery", async () => {
    const io = transport();
    const stub = companion("cancelled-before-delivery");
    await stub.importSnapshot(library(2));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    await stub.enqueueTelegramUpdate(message(2, "/endstudy"));
    await drain(stub);

    expect(io.sent).toEqual(["Review exited. Unfinished cards are still due."]);
  });

  it("delivers, grades an answer, applies the rating, and buries the entry's siblings", async () => {
    const io = transport();
    const stub = companion("graded");
    await stub.importSnapshot(library(3));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    await drain(stub);
    expect(io.sent[0]).toContain("Review 1 of 3 · 3 due");
    expect(io.sent[0]).toContain("What does 'word-1' mean?");

    await stub.enqueueTelegramUpdate(message(2, "hint"));
    await drain(stub);
    expect(io.sent[1]).toBe("Hint: Example 1.");
    expect(io.modelCalls()).toBe(0);

    await stub.enqueueTelegramUpdate(message(3, "a street of some kind"));
    await drain(stub);
    expect(io.modelCalls()).toBe(1);
    expect(io.sent[2]).toContain("Grade: Correct");
    expect(io.sent[2]).toContain("Feedback: Accurate.");
    expect(io.sent[2]).toContain("Choose effort: Hard or Good or Easy.");

    await stub.enqueueTelegramUpdate(message(4, "nonsense"));
    await drain(stub);
    expect(io.sent[3]).toBe("Send one of the listed effort ratings.");

    await stub.enqueueTelegramUpdate(message(5, "Good"));
    await drain(stub);
    expect(io.sent[4]).toContain("Rated: Good");
    expect(io.sent[4]).toContain("Next due: ");
    expect(io.sent[4]).toContain("Progress: 1 of 3 complete.");
    // The next prompt rides along and is itself only answerable once delivered.
    expect(io.sent[4]).toContain("What does 'word-2' mean?");

    const exported = (await stub.exportSnapshot())!;
    const forward = exported.cards.find((card) => card.id === 1)!;
    expect(forward.state).toBe("review");
    expect(forward.repetitions).toBe(1);
    expect(exported.cards.find((card) => card.id === 2)!.buriedUntilLocalDate).toBe("2026-07-23");
    expect(exported.reviewAttempts).toHaveLength(1);
    expect(exported.reviewAttempts[0]).toMatchObject({
      rating: "good",
      evaluatorGrade: "correct",
      submittedAnswer: "a street of some kind",
    });
    const delivered = exported.deliveryAttempts.filter((attempt) => attempt.status === "delivered");
    expect(delivered).toHaveLength(2);
    expect(delivered[0]!.contentFingerprint).toMatch(/^[0-9a-f]{64}$/u);
  });

  it("adds an incorrect daily-review card once at the queue tail", async () => {
    const io = transport();
    const stub = companion("daily-review-tail-retry");
    await stub.importSnapshot(library(2));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    await drain(stub);
    expect(io.sent[0]).toContain("What does 'word-1' mean?");

    io.evaluation = { grade: "incorrect", feedback: "Wrong." };
    await stub.enqueueTelegramUpdate(message(2, "wrong"));
    await drain(stub);
    expect(io.sent[1]).toContain("Rated: Again");
    expect(io.sent[1]).toContain("Retry added at the end.");
    expect(io.sent[1]).toContain("What does 'word-2' mean?");

    io.evaluation = { grade: "correct", feedback: "Accurate." };
    await stub.enqueueTelegramUpdate(message(3, "definition two"));
    await drain(stub);
    await stub.enqueueTelegramUpdate(message(4, "good"));
    await drain(stub);
    expect(io.sent[3]).toContain("Review 3 of 3 · 1 due · retry");
    expect(io.sent[3]).toContain("What does 'word-1' mean?");
  });

  it("explains a provider limit and grades the resubmission after recovery", async () => {
    const io = transport();
    const stub = companion("provider-limit");
    await stub.importSnapshot(library(1));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    await drain(stub);

    io.modelStatus = 429;
    await stub.enqueueTelegramUpdate(message(2, "definition one"));
    await drain(stub);

    expect(io.modelCalls()).toBe(3);
    expect(io.sent[1]).toBe(
      "OpenCode's rate or usage limit was reached. Nothing was recorded. " +
        "Try again after the limit resets or usage is restored — the next message you send is graded as your answer.",
    );
    expect((await stub.exportSnapshot())!.answerDrafts).toHaveLength(0);

    io.modelStatus = 200;
    await stub.enqueueTelegramUpdate(message(3, "definition one"));
    await drain(stub);

    expect(io.modelCalls()).toBe(4);
    expect(io.sent[2]).toContain("Grade: Correct");
    expect((await stub.exportSnapshot())!.answerDrafts).toHaveLength(1);
  });

  it("surfaces due work as an interruption when no tick ever prepared a prompt", async () => {
    const io = transport();
    const stub = companion("cold-backlog");
    const snapshot = library(2);
    await stub.importSnapshot({
      ...snapshot,
      cards: [
        {
          ...snapshot.cards[0]!,
          state: "review" as const,
          stability: encodeReal(3.5),
          difficulty: encodeReal(5),
          dueAt: "2026-07-18T00:00:00Z",
          effectiveDueAt: "2026-07-18T00:00:00Z",
          lastReviewAt: "2026-07-15T00:00:00Z",
          repetitions: 1,
          introducedLocalDate: "2026-07-15",
        },
        ...snapshot.cards.slice(1),
      ],
    });

    await stub.enqueueTelegramUpdate(message(1, "obdurate"));
    await drain(stub);

    expect(io.modelCalls()).toBe(0);
    expect(io.sent[0]).toContain("Review due. Answer this delivered question first:");
    expect(io.sent[0]).toContain("Your original message was:\nobdurate");
    expect(io.sent[0]).toContain("Complete or exit the review, then resubmit it.");
    expect((await stub.summary()).entries).toBe(2);
  });

  it("captures the resubmitted word after /endstudy instead of re-arming the review", async () => {
    // R4 promises resubmission works after an exit. Re-arming on the very next
    // message made the bot's own instruction — "exit the review, then resubmit
    // it" — impossible to follow, and no word could be saved while due work
    // existed, which is almost always.
    const io = transport();
    const stub = companion("exit-then-capture");
    const snapshot = library(2);
    await stub.importSnapshot({
      ...snapshot,
      cards: [
        {
          ...snapshot.cards[0]!,
          state: "review" as const,
          stability: encodeReal(3.5),
          difficulty: encodeReal(5),
          dueAt: "2026-07-18T00:00:00Z",
          effectiveDueAt: "2026-07-18T00:00:00Z",
          lastReviewAt: "2026-07-15T00:00:00Z",
          repetitions: 1,
          introducedLocalDate: "2026-07-15",
        },
        ...snapshot.cards.slice(1),
      ],
    });

    await stub.enqueueTelegramUpdate(message(1, "obdurate"));
    await drain(stub);
    expect(io.sent[0]).toContain("Review due. Answer this delivered question first:");
    expect((await stub.summary()).entries).toBe(2);

    await stub.enqueueTelegramUpdate(message(2, "/endstudy"));
    await drain(stub);
    expect(io.sent[1]).toBe("Review exited. Unfinished cards are still due.");

    await stub.enqueueTelegramUpdate(message(3, "obdurate"));
    await drain(stub);

    expect(io.sent[2]).not.toContain("Answer this delivered question first");
    expect(io.sent[2]).toContain("obdurate");
    expect((await stub.summary()).entries).toBe(3);
  });

  it("grades punctuation-equivalent reverse answers without calling the evaluator", async () => {
    expect(normalizeReverseAnswer("  Pro-forma...  ")).toBe("proforma");
    expect(normalizeReverseAnswer("can't")).toBe(normalizeReverseAnswer("cant"));
    expect(normalizeReverseAnswer("C++")).toBe(normalizeReverseAnswer("C"));

    const io = transport();
    const stub = companion("reverse-test");
    const snapshot = library(5);
    await stub.importSnapshot({
      ...snapshot,
      entries: snapshot.entries.map((entry, index) =>
        index === 0
          ? { ...entry, displayText: "Pro-forma", normalizedText: "pro-forma" }
          : entry,
      ),
    });

    await stub.enqueueTelegramUpdate(message(1, "/test reverse"));
    await drain(stub);
    expect(io.sent[0]).toBe(
      "Question 1 of 5\nWhich saved word matches this definition?\ndefinition 1",
    );

    await stub.enqueueTelegramUpdate(message(2, "pro forma"));
    await drain(stub);
    expect(io.modelCalls()).toBe(0);
    expect(io.sent[1]).toContain("Grade: Correct\nFeedback: Exact match to the saved entry.");
    expect(io.sent[1]).toContain("Answer: Pro-forma");

    await stub.enqueueTelegramUpdate(message(3, "easy"));
    await drain(stub);
    expect(io.sent[2]).toContain("Rated: Easy");
    expect(io.sent[2]).toContain("Question 2 of 5");

    // An incorrect answer settles itself: no rating is offered and a retry is queued.
    await stub.enqueueTelegramUpdate(message(4, "not the saved word"));
    await drain(stub);
    expect(io.modelCalls()).toBe(0);
    expect(io.sent[3]).toContain("Grade: Incorrect");
    expect(io.sent[3]).toContain("That does not exactly match the saved entry.");
    expect(io.sent[3]).toContain("Retry added at the end.");
  });
});

describe("VocabularyCompanion study commands", () => {
  it("runs a five-question forward test and refuses a conflicting review", async () => {
    const io = transport();
    const stub = companion("test-command");
    await stub.importSnapshot(library(5));

    await stub.enqueueTelegramUpdate(message(1, "/test"));
    await drain(stub);
    expect(io.sent[0]).toBe(
      "Usage: /test forward|reverse\n" +
        "Forward: recall each saved meaning from its word.\n" +
        "Reverse: recall the saved word from one exact definition.",
    );

    await stub.enqueueTelegramUpdate(message(2, "/test sideways"));
    await drain(stub);
    expect(io.sent[1]).toContain("Usage: /test forward|reverse");

    await stub.enqueueTelegramUpdate(message(3, "/test forward"));
    await drain(stub);
    expect(io.sent[2]).toBe("Question 1 of 5\nWhat does 'word-1' mean?");

    await stub.enqueueTelegramUpdate(message(4, "/review"));
    await drain(stub);
    expect(io.sent[3]).toBe("Finish or exit your active test first.");
  });

  it("reports the shortfall for a test that cannot fill five distinct entries", async () => {
    const io = transport();
    const stub = companion("test-shortfall");
    await stub.importSnapshot(library(3));

    await stub.enqueueTelegramUpdate(message(1, "/test reverse"));
    await drain(stub);
    expect(io.sent[0]).toBe(
      "You have 3 eligible distinct reverse entries. Add or unbury 2 more to start.",
    );
    expect((await stub.exportSnapshot())!.studySessions).toHaveLength(0);
  });

  it("exits a study session and leaves the unanswered cards due", async () => {
    const io = transport();
    const stub = companion("endstudy");
    await stub.importSnapshot(library(2));

    await stub.enqueueTelegramUpdate(message(1, "/endstudy"));
    await drain(stub);
    expect(io.sent[0]).toBe("There is no active vocabulary study session.");

    await stub.enqueueTelegramUpdate(message(2, "/review"));
    await drain(stub);
    await stub.enqueueTelegramUpdate(message(3, "/endstudy"));
    await drain(stub);
    expect(io.sent[2]).toBe("Review exited. Unfinished cards are still due.");

    const exported = (await stub.exportSnapshot())!;
    expect(exported.cards.every((card) => card.repetitions === 0 && card.state === "new")).toBe(true);
    expect(exported.studySessions[0]!.status).toBe("exited");
    expect(exported.studyPrompts[0]!.status).toBe("cancelled");

    // The carried-over cards come back in the next session.
    await stub.enqueueTelegramUpdate(message(4, "/review"));
    await drain(stub);
    expect(io.sent[3]).toContain("What does 'word-1' mean?");
  });
});

describe("VocabularyCompanion review ticker", () => {
  it("stays silent before the review hour unless an older backlog exists", async () => {
    transport();
    const stub = companion("ticker-silent");
    await stub.importSnapshot(library(2));

    expect(await stub.enqueueDailyReview({ dedupeKey: "cron:1", nowUtc: "2026-07-20T01:00:00Z" }))
      .toBe("silent");
    expect(await stub.summary()).toMatchObject({ pendingInbox: 0 });
  });

  it("sends at most one prompt per run and never while one is in flight", async () => {
    const io = transport();
    const stub = companion("ticker-once");
    await stub.importSnapshot(library(3));

    // 04:00Z is 12:00 in Asia/Kuala_Lumpur, the configured review hour.
    expect(await stub.enqueueDailyReview({ dedupeKey: "cron:1", nowUtc: "2026-07-20T04:00:00Z" }))
      .toBe("enqueued");
    // The prompt is prepared but not yet sent, so the next run must stay quiet.
    expect(await stub.enqueueDailyReview({ dedupeKey: "cron:2", nowUtc: "2026-07-20T05:00:00Z" }))
      .toBe("silent");

    await drain(stub);
    expect(io.sent).toHaveLength(1);
    expect(io.sent[0]).toContain("What does 'word-1' mean?");

    // Now it is answerable, so later runs keep quiet rather than dumping the queue.
    expect(await stub.enqueueDailyReview({ dedupeKey: "cron:3", nowUtc: "2026-07-20T06:00:00Z" }))
      .toBe("silent");
    await drain(stub);
    expect(io.sent).toHaveLength(1);
  });

  it("prompts before the review hour when an overdue card is waiting", async () => {
    const io = transport();
    const stub = companion("ticker-backlog");
    const snapshot = library(2);
    const overdue = {
      ...snapshot.cards[0]!,
      state: "review" as const,
      stability: encodeReal(3.5),
      difficulty: encodeReal(5),
      dueAt: "2026-07-18T00:00:00Z",
      effectiveDueAt: "2026-07-18T00:00:00Z",
      lastReviewAt: "2026-07-15T00:00:00Z",
      repetitions: 1,
      introducedLocalDate: "2026-07-15",
    };
    await stub.importSnapshot({ ...snapshot, cards: [overdue, ...snapshot.cards.slice(1)] });

    expect(await stub.enqueueDailyReview({ dedupeKey: "cron:1", nowUtc: "2026-07-20T01:00:00Z" }))
      .toBe("enqueued");
    await drain(stub);
    expect(io.sent).toHaveLength(1);
    expect(io.sent[0]).toContain("What does 'word-1' mean?");
  });
});

describe("VocabularyCompanion concurrency", () => {
  it("grades the first of two racing answers and treats the second as a rating", async () => {
    const io = transport();
    const stub = companion("racing");
    await stub.importSnapshot(library(2));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    await drain(stub);

    await stub.enqueueTelegramUpdate(message(2, "stubborn"));
    await stub.enqueueTelegramUpdate(message(3, "unyielding"));
    await drain(stub);

    expect(io.modelCalls()).toBe(1);
    expect(io.sent[1]).toContain("Choose effort:");
    expect(io.sent[2]).toBe("Send one of the listed effort ratings.");
  });

  it("does not shorten a delivery retry when a duplicate update arrives", async () => {
    const io = transport();
    io.telegramFails = true;
    const stub = companion("retry-deadline");
    await stub.importSnapshot(library(1));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    const retryAt = await runInDurableObject(stub, (_instance, state) =>
      state.storage.getAlarm()
    );

    expect(await stub.enqueueTelegramUpdate(message(1, "/review"))).toBe("duplicate");
    const afterDuplicate = await runInDurableObject(stub, (_instance, state) =>
      state.storage.getAlarm()
    );

    expect(retryAt).not.toBeNull();
    expect(afterDuplicate).toBe(retryAt);
  });

  it("allows snapshot export after a terminal inbox delivery failure", async () => {
    const io = transport();
    io.telegramFails = true;
    const stub = companion("failed-export");
    await stub.importSnapshot(library(1));

    await stub.enqueueTelegramUpdate(message(1, "/review"));
    for (let attempt = 0; attempt < 10; attempt += 1) {
      expect(await runDurableObjectAlarm(stub)).toBe(true);
    }

    expect(await stub.summary()).toMatchObject({ pendingInbox: 0, failedInbox: 1 });
    expect(await stub.exportSnapshot()).not.toBeNull();
  });

  it("marks a prepared response failed after ten Telegram send failures", async () => {
    const io = transport();
    io.telegramFails = true;
    const stub = companion("failure");
    await stub.importSnapshot(library(1));

    await stub.enqueueTelegramUpdate(message(1, "/test forward"));
    for (let attempt = 0; attempt < 10; attempt += 1) {
      expect(await runDurableObjectAlarm(stub)).toBe(true);
    }
    expect(await stub.summary()).toMatchObject({ pendingInbox: 0, failedInbox: 1 });
  });
});

describe("VocabularyCompanion admin entry repair", () => {
  it("replaces a mis-entered display and normalized text in place", async () => {
    const stub = companion("fix-entry");
    await stub.importSnapshot({
      ...library(1),
      entries: [
        {
          id: 1,
          displayText: "Abscond\nAbscond",
          normalizedText: "abscond abscond",
          dateAdded: "2026-07-19T12:45:48.055085Z",
          lastReviewed: null,
          reviewStatus: "new",
        },
      ],
    });

    expect(await stub.fixEntry({ id: 1, displayText: "Abscond" })).toEqual({
      status: "updated",
      displayText: "Abscond",
      normalizedText: "abscond",
    });

    const exported = (await stub.exportSnapshot())!;
    expect(exported.entries).toEqual([
      expect.objectContaining({ id: 1, displayText: "Abscond", normalizedText: "abscond" }),
    ]);
    // Senses, cards, and every audit row survive untouched.
    expect(exported.senses).toHaveLength(1);
    expect(exported.cards).toHaveLength(2);
  });

  it("rejects an unknown id and a normalized-text collision", async () => {
    const stub = companion("fix-entry-miss");
    await stub.importSnapshot(library(2));

    expect(await stub.fixEntry({ id: 99, displayText: "word-9" })).toEqual({
      status: "not_found",
    });
    // "word-2" normalizes to entry 2's normalized text, which is taken.
    expect(await stub.fixEntry({ id: 1, displayText: "word-2" })).toEqual({
      status: "conflict",
    });
  });
});
