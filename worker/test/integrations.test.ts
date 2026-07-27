import { afterEach, describe, expect, it, vi } from "vitest";

import { EvaluationGrade, type VocabularyEntry } from "../src/domain/models";
import {
  DefinitionStatus,
  EvaluationStatus,
  OpenCodeAdapter,
  SHOW_ANSWER_FEEDBACK,
  parseDefinitionResponse,
  parseEvaluationResponse,
} from "../src/integrations/opencode";
import { splitTelegramMessage, TelegramAdapter } from "../src/integrations/telegram";

const ENTRY: VocabularyEntry = {
  id: 1,
  displayText: "obdurate",
  normalizedText: "obdurate",
  dateAdded: "2026-07-20T00:00:00Z",
  lastReviewed: null,
  reviewStatus: "new",
  senses: [{
    id: 1,
    entryId: 1,
    definition: "stubbornly refusing to change",
    partOfSpeech: "adjective",
    exampleSentence: "He remained obdurate.",
    sourceContext: null,
    dateAdded: "2026-07-20T00:00:00Z",
  }],
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("OpenCodeAdapter", () => {
  it("strictly parses definitions after validating every sense and deduplicates in order", () => {
    const result = parseDefinitionResponse(JSON.stringify({ senses: [
      { part_of_speech: " noun ", definition: " first ", example_sentence: " one " },
      { part_of_speech: "NOUN", definition: "FIRST", example_sentence: "duplicate" },
    ] }));
    expect(result).toEqual({
      status: DefinitionStatus.FOUND,
      cards: [{ partOfSpeech: "noun", definition: "first", exampleSentence: "one" }],
    });
    expect(parseDefinitionResponse('{"status":"not_found"}').status).toBe(DefinitionStatus.NOT_FOUND);
    expect(parseDefinitionResponse('{"senses":[],"extra":1}').status).toBe(DefinitionStatus.INVALID_RESPONSE);
  });

  it("makes one bounded tool-free request and accepts only choices message content", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      choices: [{ message: { content: '{"senses":[{"part_of_speech":"noun","definition":"x","example_sentence":"y"}]}' } }],
    }));
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({ apiKey: "key", baseUrl: "https://example.test/v1", model: "model" });
    expect((await adapter.defineEntry("word")).status).toBe(DefinitionStatus.FOUND);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const request = fetchMock.mock.calls[0]![1] as RequestInit;
    expect(JSON.parse(request.body as string)).toMatchObject({
      model: "model",
      max_tokens: 4_000,
      temperature: 0,
      tools: [],
    });
  });

  it("uses exact-only show answer without a provider call and strictly grades other answers", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({ apiKey: "key", baseUrl: "https://example.test/v1", model: "model" });
    expect(await adapter.evaluateAnswer(ENTRY, "show answer")).toEqual({
      status: EvaluationStatus.VALID,
      evaluation: { grade: EvaluationGrade.INCORRECT, feedback: SHOW_ANSWER_FEEDBACK },
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect((await adapter.evaluateAnswer(ENTRY, "\u001c\u001d\u001e\u001f")).status)
      .toBe(EvaluationStatus.INVALID_RESPONSE);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(parseEvaluationResponse('{"grade":"correct","feedback":" Good. "}')).toEqual({
      status: EvaluationStatus.VALID,
      evaluation: { grade: EvaluationGrade.CORRECT, feedback: "Good." },
    });
    expect(parseEvaluationResponse('{"grade":"correct","feedback":"ok","extra":1}').status)
      .toBe(EvaluationStatus.INVALID_RESPONSE);
  });
});

describe("TelegramAdapter", () => {
  it("splits at paragraph and newline boundaries before code-point-safe hard splits", () => {
    expect(splitTelegramMessage("one\n\ntwo", 6)).toEqual(["one\n\n", "two"]);
    expect(splitTelegramMessage("😀😀😀", 2)).toEqual(["😀😀", "😀"]);
  });

  it("sends chunks sequentially, returns their message ids, and rejects Telegram errors", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ ok: true, result: { message_id: 41 } }))
      .mockResolvedValueOnce(response({ ok: false }));
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new TelegramAdapter({ botToken: "token", chatId: "123" });
    await expect(adapter.sendText("one\n\ntwo")).resolves.toEqual([41]);
    const body = JSON.parse((fetchMock.mock.calls[0]![1] as RequestInit).body as string);
    expect(body).toEqual({ chat_id: "123", text: "one\n\ntwo" });
    await expect(adapter.sendText("again")).rejects.toThrow(/rejected/u);
  });
});
