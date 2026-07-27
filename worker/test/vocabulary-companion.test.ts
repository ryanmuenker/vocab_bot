import { env } from "cloudflare:workers";
import { runDurableObjectAlarm } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EvaluationGrade, ReviewCompletionStatus } from "../src/domain/models";
import { formatReviewCompletion } from "../src/domain/formatting";
import { sha256Snapshot, type SnapshotV1 } from "../src/domain/snapshot";

function message(updateId: number, text: string) {
  return {
    updateId,
    messageId: updateId,
    chatId: "123456",
    senderId: "123456",
    text,
    receivedAt: "2026-07-23T00:00:00Z",
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("VocabularyCompanion durable inbox", () => {
  it("deduplicates updates and coalesces equivalent unseen captures into one model call", async () => {
    const requests: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push(url);
      if (url.includes("/chat/completions")) {
        return jsonResponse({ choices: [{ message: { content: JSON.stringify({ senses: [{
          part_of_speech: "noun",
          definition: "a street",
          example_sentence: "The street was quiet.",
        }] }) } }] });
      }
      expect(JSON.parse(init!.body as string)).toMatchObject({ chat_id: "123456" });
      return jsonResponse({ ok: true });
    }));
    const stub = env.VOCABULARY.getByName(`coalesce-${crypto.randomUUID()}`);
    expect(await stub.enqueueTelegramUpdate(message(1, "Straße"))).toBe("enqueued");
    expect(await stub.enqueueTelegramUpdate(message(1, "Straße"))).toBe("duplicate");
    expect(await stub.enqueueTelegramUpdate(message(2, "  STRASSE  "))).toBe("enqueued");
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(requests.filter((url) => url.includes("/chat/completions"))).toHaveLength(1);
    expect(requests.filter((url) => url.includes("api.telegram.org"))).toHaveLength(2);
    expect(await stub.summary()).toMatchObject({
      entries: 1,
      senses: 1,
      pendingInbox: 0,
      failedInbox: 0,
    });
  });

  it("snapshots two concurrent review answers to one event and advances only once", async () => {
    const snapshot: SnapshotV1 = {
      formatVersion: 1,
      entries: [{
        id: 1,
        displayText: "obdurate",
        normalizedText: "obdurate",
        dateAdded: "2026-07-20T00:00:00Z",
        lastReviewed: null,
        reviewStatus: "new",
      }],
      senses: [{
        id: 1,
        entryId: 1,
        definition: "stubbornly refusing to change",
        partOfSpeech: "adjective",
        exampleSentence: "He remained obdurate.",
        sourceContext: null,
        dateAdded: "2026-07-20T00:00:00Z",
      }],
      reviewEvents: [{
        id: 1,
        entryId: 1,
        reviewDate: "2026-07-23",
        status: "pending",
        promptedAt: "2026-07-22T16:00:00Z",
        answeredAt: null,
        answerText: null,
        grade: null,
        evaluationFeedback: null,
      }],
      testSessions: [],
      testQuestions: [],
    };
    const sent: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/chat/completions")) {
        return jsonResponse({ choices: [{ message: { content: JSON.stringify({
          grade: EvaluationGrade.CORRECT,
          feedback: "Accurate.",
        }) } }] });
      }
      sent.push(JSON.parse(init!.body as string).text as string);
      return jsonResponse({ ok: true });
    }));
    const stub = env.VOCABULARY.getByName(`review-${crypto.randomUUID()}`);
    await stub.importSnapshot(snapshot, await sha256Snapshot(snapshot));
    await stub.enqueueTelegramUpdate(message(10, "stubborn"));
    await stub.enqueueTelegramUpdate(message(11, "unyielding"));
    await runDurableObjectAlarm(stub);
    await runDurableObjectAlarm(stub);
    expect(sent.some((text) => text.includes("Accurate."))).toBe(true);
    expect(sent).toContain(formatReviewCompletion({ status: ReviewCompletionStatus.NO_PENDING }));
    expect((await stub.exportSnapshot())!.reviewEvents[0]).toMatchObject({
      status: "answered",
      answerText: "stubborn",
      grade: "correct",
    });
  });

  it("copies a prepared capture response to late coalesced followers", async () => {
    let modelCalls = 0;
    let telegramCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/chat/completions")) {
        modelCalls += 1;
        return jsonResponse({ choices: [{ message: { content: JSON.stringify({ senses: [{
          part_of_speech: "noun",
          definition: "a street",
          example_sentence: "The street was quiet.",
        }] }) } }] });
      }
      telegramCalls += 1;
      return jsonResponse({ ok: telegramCalls !== 1 });
    }));
    const stub = env.VOCABULARY.getByName(`late-coalesce-${crypto.randomUUID()}`);
    await stub.enqueueTelegramUpdate(message(20, "Straße"));
    await runDurableObjectAlarm(stub);
    await stub.enqueueTelegramUpdate(message(21, "STRASSE"));
    await runDurableObjectAlarm(stub);
    await runDurableObjectAlarm(stub);
    expect(modelCalls).toBe(1);
    expect(telegramCalls).toBe(3);
    expect(await stub.summary()).toMatchObject({ pendingInbox: 0, failedInbox: 0 });
  });

  it("resets Telegram retry accounting after each delivered chunk", async () => {
    let telegramCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/chat/completions")) {
        const senses = Array.from({ length: 10 }, (_, index) => ({
          part_of_speech: "noun",
          definition: `${index} ${"d".repeat(450)}`,
          example_sentence: `${index} ${"e".repeat(450)}`,
        }));
        return jsonResponse({ choices: [{ message: { content: JSON.stringify({ senses }) } }] });
      }
      telegramCalls += 1;
      return jsonResponse({ ok: telegramCalls === 10 || telegramCalls > 11 });
    }));
    const stub = env.VOCABULARY.getByName(`chunk-retries-${crypto.randomUUID()}`);
    await stub.enqueueTelegramUpdate(message(30, "long-response"));
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const summary = await stub.summary();
      if (summary.pendingInbox === 0) break;
      await runDurableObjectAlarm(stub);
    }
    expect(telegramCalls).toBeGreaterThan(11);
    expect(await stub.summary()).toMatchObject({ pendingInbox: 0, failedInbox: 0 });
  });


  it("marks a prepared response failed after ten Telegram send failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ ok: false })));
    const stub = env.VOCABULARY.getByName(`failure-${crypto.randomUUID()}`);
    await stub.enqueueTelegramUpdate(message(20, "/test now"));
    for (let attempt = 0; attempt < 10; attempt += 1) {
      expect(await runDurableObjectAlarm(stub)).toBe(true);
    }
    expect(await stub.summary()).toMatchObject({ pendingInbox: 0, failedInbox: 1 });
  });
});
