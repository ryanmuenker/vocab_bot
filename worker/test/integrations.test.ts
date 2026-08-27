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

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("OpenCodeAdapter", () => {
  it("preserves old found and not-found contracts while deduplicating senses in order", () => {
    const result = parseDefinitionResponse(JSON.stringify({ senses: [
      { part_of_speech: " noun ", definition: " first ", example_sentence: " one " },
      { part_of_speech: "NOUN", definition: "FIRST", example_sentence: "duplicate" },
    ] }), "word");
    expect(result).toEqual({
      status: DefinitionStatus.FOUND,
      cards: [{ partOfSpeech: "noun", definition: "first", exampleSentence: "one" }],
      visualIntent: null,
    });
    expect(parseDefinitionResponse('{"status":"not_found"}', "word")).toEqual({
      status: DefinitionStatus.NOT_FOUND,
      cards: [],
      visualIntent: null,
    });
  });

  it("parses one sense-grounded Doric visual intent and keeps aster eligible", () => {
    const doric = parseDefinitionResponse(JSON.stringify({
      senses: [{
        part_of_speech: "adjective",
        definition: "Relating to the Greek architectural order with plain column capitals.",
        example_sentence: "The temple has a Doric colonnade.",
      }],
      visual: {
        sense_index: 0,
        category: "architecture",
        query: "Doric order columns",
        description: "Plain columns of the Doric architectural order.",
      },
    }), "Doric");
    expect(doric).toMatchObject({
      status: DefinitionStatus.FOUND,
      visualIntent: {
        senseIndex: 0,
        category: "architecture",
        query: "Doric order columns",
      },
    });

    const aster = parseDefinitionResponse(JSON.stringify({
      senses: [{
        part_of_speech: "noun",
        definition: "A flowering plant with daisy-like blossoms.",
        example_sentence: "The asters bloomed in autumn.",
      }],
      visual: {
        sense_index: 0,
        category: "plant",
        query: "aster flowering plant",
        description: "Purple flowers on an aster plant.",
      },
    }), "aster");
    expect(aster.visualIntent).not.toBeNull();
  });

  it("isolates malformed optional visual metadata from valid ordered senses", () => {
    const senses = [{
      part_of_speech: "noun",
      definition: "A flowering plant with daisy-like blossoms.",
      example_sentence: "The asters bloomed in autumn.",
    }];
    const cards = [{
      partOfSpeech: "noun",
      definition: "A flowering plant with daisy-like blossoms.",
      exampleSentence: "The asters bloomed in autumn.",
    }];
    const malformed = [
      { category: "plant", query: "aster plant", description: "An aster flower." },
      { sense_index: 4, category: "plant", query: "aster plant", description: "An aster flower." },
      { sense_index: [0, 1], category: "plant", query: "aster plant", description: "An aster flower." },
      { sense_index: 0, category: "abstract", query: "aster plant", description: "An aster flower." },
      { sense_index: 0, category: "plant", query: "incategory:Plants", description: "An aster flower." },
      { sense_index: 0, category: "plant", query: "aster plant", description: 7 },
      { sense_index: 0, category: "plant", query: "aster plant", description: "An aster flower.", extra: true },
    ];
    for (const visual of malformed) {
      expect(parseDefinitionResponse(JSON.stringify({ senses, visual }), "aster")).toEqual({
        status: DefinitionStatus.FOUND,
        cards,
        visualIntent: null,
      });
    }
  });

  it("keeps malformed senses invalid even when optional visual metadata is valid", () => {
    expect(parseDefinitionResponse(JSON.stringify({
      senses: [{
        part_of_speech: "noun",
        definition: "",
        example_sentence: "The asters bloomed in autumn.",
      }],
      visual: {
        sense_index: 0,
        category: "plant",
        query: "aster flowering plant",
        description: "Purple flowers on an aster plant.",
      },
    }), "aster")).toEqual({
      status: DefinitionStatus.INVALID_RESPONSE,
      cards: [],
      visualIntent: null,
    });
  });

  it("keeps ambiguous, abstract, and sensitive definitions text-only", () => {
    const candidate = {
      sense_index: 0,
      category: "animal",
      query: "crane bird",
      description: "A tall crane bird in water.",
    };
    const crane = parseDefinitionResponse(JSON.stringify({
      senses: [
        {
          part_of_speech: "noun",
          definition: "A tall wading bird with a long neck.",
          example_sentence: "A crane stood in the marsh.",
        },
        {
          part_of_speech: "noun",
          definition: "A large machine used for lifting heavy objects.",
          example_sentence: "The crane lifted the beam.",
        },
      ],
      visual: candidate,
    }), "crane");
    expect(crane).toMatchObject({ status: DefinitionStatus.FOUND, visualIntent: null });

    for (const [entry, definition] of [
      ["duplicity", "Deceitful conduct or double-dealing."],
      ["paraphimosis", "A medical condition involving the foreskin."],
    ] as const) {
      const result = parseDefinitionResponse(JSON.stringify({
        senses: [{
          part_of_speech: "noun",
          definition,
          example_sentence: `The term was ${entry}.`,
        }],
        visual: {
          sense_index: 0,
          category: "visual style",
          query: `${entry} visual style`,
          description: `A visual representation of ${entry}.`,
        },
      }), entry);
      expect(result).toMatchObject({ status: DefinitionStatus.FOUND, visualIntent: null });
    }
  });

  it("retains strict top-level and core-field validation", () => {
    expect(parseDefinitionResponse('{"senses":[],"extra":1}', "word").status)
      .toBe(DefinitionStatus.INVALID_RESPONSE);
    expect(parseDefinitionResponse('{"status":"not_found","visual":null}', "word").status)
      .toBe(DefinitionStatus.INVALID_RESPONSE);
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
    const body = JSON.parse(request.body as string) as {
      messages: { role: string; content: string }[];
    } & Record<string, unknown>;
    expect(body).toMatchObject({
      model: "model",
      max_tokens: 4_000,
      temperature: 0,
      tools: [],
    });
    expect(body.messages[0]!.content).toContain("most common");
    expect(body.messages[0]!.content).toContain("first three");
    expect(body.messages[0]!.content).toContain("semantically distinct");
    expect(body.messages[0]!.content).toContain("sense_index");
    expect(body.messages[0]!.content).toContain("medical/anatomy");
    expect(body.messages[0]!.content).toContain("Omit visual");
  });

  it("uses the Responses API required by GPT 5.6 Luna", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      output: [{
        type: "message",
        content: [{
          type: "output_text",
          text: JSON.stringify({ grade: "correct", feedback: "Accurate." }),
        }],
      }],
    }));
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({
      apiKey: "key",
      baseUrl: "https://example.test/v1",
      model: "gpt-5.6-luna",
    });

    expect(await adapter.evaluateAnswer(
      ENTRY,
      "It means stubbornly refusing to change one's opinion.",
    )).toEqual({
      status: EvaluationStatus.VALID,
      evaluation: { grade: EvaluationGrade.CORRECT, feedback: "Accurate." },
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0]![0]).toBe("https://example.test/v1/responses");
    const request = fetchMock.mock.calls[0]![1] as RequestInit;
    const body = JSON.parse(request.body as string) as Record<string, unknown>;
    expect(body).toMatchObject({
      model: "gpt-5.6-luna",
      max_output_tokens: 4_000,
    });
    expect(body.instructions).toContain("semantic paraphrase");
    expect(body.input).toContain("stubbornly refusing");
    expect(body).not.toHaveProperty("messages");
    expect(body).not.toHaveProperty("max_tokens");
  });

  it("retries two transient definition failures before succeeding", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({}, 500))
      .mockResolvedValueOnce(response({}, 503))
      .mockResolvedValueOnce(response({
        choices: [{
          message: {
            content: JSON.stringify({
              senses: [{
                part_of_speech: "noun",
                definition: "The act of lying face downward.",
                example_sentence: "The worshippers performed prostration.",
              }],
            }),
          },
        }],
      }));
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({
      apiKey: "key",
      baseUrl: "https://example.test/v1",
      model: "model",
    });

    const definition = await adapter.defineEntry("Prostration");

    expect(definition).toMatchObject({ status: DefinitionStatus.FOUND });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("logs retry metadata without the submitted vocabulary text", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({}, 500));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({
      apiKey: "key",
      baseUrl: "https://example.test/v1",
      model: "model",
    });

    const definition = await adapter.defineEntry("Prostration");

    expect(definition).toEqual({
      status: DefinitionStatus.PROVIDER_ERROR,
      cards: [],
      visualIntent: null,
    });
    expect(warn).toHaveBeenCalledTimes(3);
    expect(warn).toHaveBeenLastCalledWith({
      event: "opencode_chat_failure",
      kind: "http",
      status: 500,
      attempt: 3,
      maxAttempts: 3,
    });
    expect(JSON.stringify(warn.mock.calls)).not.toContain("Prostration");
  });

  it("classifies a persistent evaluation rate limit after exhausting retries", async () => {
    const answerText = "It means stubbornly refusing to change one's opinion.";
    const fetchMock = vi.fn().mockResolvedValue(response({}, 429));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({
      apiKey: "key",
      baseUrl: "https://example.test/v1",
      model: "model",
    });

    expect(await adapter.evaluateAnswer(ENTRY, answerText)).toEqual({
      status: EvaluationStatus.RATE_LIMITED,
      evaluation: null,
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(warn).toHaveBeenLastCalledWith({
      event: "opencode_chat_failure",
      kind: "http",
      status: 429,
      attempt: 3,
      maxAttempts: 3,
    });
    expect(JSON.stringify(warn.mock.calls)).not.toContain(answerText);
  });

  it("classifies malformed successful chat JSON without retrying it as a network failure", async () => {
    const fetchMock = vi.fn().mockImplementation(
      () => Promise.resolve(new Response("{", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })),
    );
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({
      apiKey: "key",
      baseUrl: "https://example.test/v1",
      model: "model",
    });

    expect(await adapter.evaluateAnswer(ENTRY, "A substantive learner answer.")).toEqual({
      status: EvaluationStatus.PROVIDER_ERROR,
      evaluation: null,
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(warn).not.toHaveBeenCalled();
  });

  it("retries a successful response read failure before evaluating later content", async () => {
    const answerText = "It means stubbornly refusing to change one's opinion.";
    const unreadableResponse = response({});
    vi.spyOn(unreadableResponse, "json").mockRejectedValue(
      new TypeError("response body stream failed"),
    );
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(unreadableResponse)
      .mockResolvedValueOnce(response({
        choices: [{
          message: {
            content: JSON.stringify({ grade: "correct", feedback: "Good." }),
          },
        }],
      }));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({
      apiKey: "key",
      baseUrl: "https://example.test/v1",
      model: "model",
    });

    expect(await adapter.evaluateAnswer(ENTRY, answerText)).toEqual({
      status: EvaluationStatus.VALID,
      evaluation: { grade: EvaluationGrade.CORRECT, feedback: "Good." },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(warn).toHaveBeenCalledWith({
      event: "opencode_chat_failure",
      kind: "network",
      attempt: 1,
      maxAttempts: 3,
    });
    expect(JSON.stringify(warn.mock.calls)).not.toContain(answerText);
  });

  it("classifies invalid definition JSON without logging the submitted text", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      choices: [{ message: { content: "not json" } }],
    }));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    const adapter = new OpenCodeAdapter({
      apiKey: "key",
      baseUrl: "https://example.test/v1",
      model: "model",
    });

    expect(await adapter.defineEntry("Prostration")).toEqual({
      status: DefinitionStatus.INVALID_RESPONSE,
      cards: [],
      visualIntent: null,
    });
    expect(warn).toHaveBeenCalledOnce();
    expect(warn).toHaveBeenCalledWith({
      event: "opencode_definition_failure",
      kind: "invalid_response",
    });
    expect(JSON.stringify(warn.mock.calls)).not.toContain("Prostration");
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

  it("rejects a successful Telegram response without a message id", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      response({ ok: true, result: {} }),
    ));
    const adapter = new TelegramAdapter({ botToken: "token", chatId: "123" });

    await expect(adapter.sendText("hello")).rejects.toThrow(/message id/u);
  });
});
