import { describe, expect, it } from "vitest";

import { CaptureOperation, EntryTextStatus, VisualCategory } from "../src/domain/models";
import type { CaptureCommand, SenseCard } from "../src/domain/models";
import {
  MAX_ENTRY_TEXT_LENGTH,
  MAX_PART_OF_SPEECH_LENGTH,
  MAX_SENSE_COUNT,
  MAX_SENSE_TEXT_LENGTH,
  MAX_SOURCE_CONTEXT_LENGTH,
  caseFold,
  normalizeEntryText,
  normalizeSenseIdentity,
  parseCaptureMessage,
  prepareCaptureCommand,
  validateSenseCards,
} from "../src/domain/normalization";
import {
  MAX_VISUAL_DESCRIPTION_LENGTH,
  MAX_VISUAL_QUERY_LENGTH,
  decodeVisualIntent,
  encodeVisualIntent,
  validateVisualIntent,
} from "../src/domain/visual-enrichment";

const DORIC_SENSES: readonly SenseCard[] = [{
  partOfSpeech: "adjective",
  definition: "Relating to the ancient Greek architectural order with plain column capitals.",
  exampleSentence: "The temple has a Doric colonnade.",
}];

const ASTER_SENSES: readonly SenseCard[] = [{
  partOfSpeech: "noun",
  definition: "A flowering plant with daisy-like blossoms.",
  exampleSentence: "Purple asters flowered beside the path.",
}];

const OBJECT_SENSES: readonly SenseCard[] = [{
  partOfSpeech: "noun",
  definition: "A small carved object.",
  exampleSentence: "The object was displayed.",
}];

describe("visual enrichment policy", () => {
  it("fixes the initial eligible category boundary", () => {
    expect(Object.values(VisualCategory)).toEqual([
      "plant",
      "animal",
      "architecture",
      "object",
      "material",
      "place",
      "garment",
      "food",
      "vehicle",
      "instrument",
      "landform",
      "visual style",
    ]);
  });

  it("accepts a bounded Doric architectural intent grounded in one sense", () => {
    expect(validateVisualIntent("Doric", DORIC_SENSES, {
      sense_index: 0,
      category: "architecture",
      query: "Doric order columns",
      description: "Plain capitals and fluted columns of the Doric architectural order.",
    })).toEqual({
      senseIndex: 0,
      category: VisualCategory.ARCHITECTURE,
      query: "Doric order columns",
      description: "Plain capitals and fluted columns of the Doric architectural order.",
    });
  });

  it("accepts the dominant plant sense of aster", () => {
    expect(validateVisualIntent("aster", ASTER_SENSES, {
      sense_index: 0,
      category: "plant",
      query: "aster flowering plant",
      description: "Purple flowers on an aster plant.",
    })).toMatchObject({
      senseIndex: 0,
      category: VisualCategory.PLANT,
    });
  });

  it("rejects competing unrelated visual senses and ambiguity claims", () => {
    const craneSenses: readonly SenseCard[] = [
      {
        partOfSpeech: "noun",
        definition: "A tall wading bird with a long neck.",
        exampleSentence: "A crane stood in the marsh.",
      },
      {
        partOfSpeech: "noun",
        definition: "A large machine used for lifting heavy objects.",
        exampleSentence: "The crane lifted the beam.",
      },
    ];
    const candidate = {
      sense_index: 0,
      category: "animal",
      query: "crane bird",
      description: "A tall crane bird standing in water.",
    };
    expect(validateVisualIntent("crane", craneSenses, candidate)).toBeNull();
    expect(validateVisualIntent("aster", ASTER_SENSES, {
      ...candidate,
      query: "ambiguous aster image",
      description: "No dominant visual referent.",
    })).toBeNull();
  });

  it("keeps duplicity and every sensitive paraphimosis surface text-only", () => {
    expect(validateVisualIntent("duplicity", [{
      partOfSpeech: "noun",
      definition: "Deceitful conduct or double-dealing.",
      exampleSentence: "The scheme depended on duplicity.",
    }], {
      sense_index: 0,
      category: "visual style",
      query: "duplicity visual pattern",
      description: "A visual pattern representing duplicity.",
    })).toBeNull();

    const neutralObjectSense = OBJECT_SENSES;
    expect(validateVisualIntent("paraphimosis", neutralObjectSense, {
      sense_index: 0,
      category: "object",
      query: "carved object",
      description: "A small carved object.",
    })).toBeNull();
    expect(validateVisualIntent("token", [{
      ...neutralObjectSense[0]!,
      definition: "An object used during a surgical procedure.",
    }], {
      sense_index: 0,
      category: "object",
      query: "carved object",
      description: "A small carved object.",
    })).toBeNull();
    expect(validateVisualIntent("scalpel", [{
      ...neutralObjectSense[0]!,
      definition: "A small sharp knife used by a surgeon.",
    }], {
      sense_index: 0,
      category: "object",
      query: "scalpel tool",
      description: "A scalpel object tool.",
    })).toBeNull();
    expect(validateVisualIntent("porn", neutralObjectSense, {
      sense_index: 0,
      category: "object",
      query: "adult visual media",
      description: "A sexually explicit visual object.",
    })).toBeNull();
    expect(validateVisualIntent("token", neutralObjectSense, {
      sense_index: 0,
      category: "object",
      query: "medical object",
      description: "A small carved object.",
    })).toBeNull();
    expect(validateVisualIntent("token", neutralObjectSense, {
      sense_index: 0,
      category: "object",
      query: "carved object",
      description: "An object beside an injured body.",
    })).toBeNull();
  });

  it("rejects every fixed text-only topic class", () => {
    const visual = {
      sense_index: 0,
      category: "object",
      query: "carved object",
      description: "A small carved object.",
    };
    for (const topic of [
      "anatomy",
      "sexual",
      "gore",
      "injury",
      "procedure",
      "person",
      "action",
      "event",
      "emotion",
      "abstract",
    ]) {
      expect(validateVisualIntent(topic, OBJECT_SENSES, visual)).toBeNull();
    }
  });

  it("rejects malformed, unbounded, and search-operator metadata", () => {
    const valid = {
      sense_index: 0,
      category: "architecture",
      query: "Doric order columns",
      description: "Doric architectural columns.",
    };
    expect(validateVisualIntent("Doric", DORIC_SENSES, { ...valid, extra: true })).toBeNull();
    expect(validateVisualIntent("Doric", DORIC_SENSES, { ...valid, sense_index: [0, 1] })).toBeNull();
    expect(validateVisualIntent("Doric", DORIC_SENSES, {
      ...valid,
      query: "incategory:Architecture Doric",
    })).toBeNull();
    expect(validateVisualIntent("Doric", DORIC_SENSES, {
      ...valid,
      query: "Doric OR Ionic",
    })).toBeNull();
    expect(validateVisualIntent("Doric", DORIC_SENSES, {
      ...valid,
      query: "Doric\ncolumns",
    })).toBeNull();
    expect(validateVisualIntent("Doric", [{
      ...DORIC_SENSES[0]!,
      definition: "Doric\u0000 architectural columns.",
    }], valid)).toBeNull();
    expect(validateVisualIntent("Doric", DORIC_SENSES, {
      ...valid,
      query: "D".repeat(MAX_VISUAL_QUERY_LENGTH + 1),
    })).toBeNull();
    expect(validateVisualIntent("Doric", DORIC_SENSES, {
      ...valid,
      description: "D".repeat(MAX_VISUAL_DESCRIPTION_LENGTH + 1),
    })).toBeNull();
  });

  it("round-trips transient intent through the domain-owned persisted representation", () => {
    const intent = validateVisualIntent("Doric", DORIC_SENSES, {
      sense_index: 0,
      category: "architecture",
      query: "Doric order columns",
      description: "Doric architectural columns.",
    })!;
    const serialized = encodeVisualIntent(intent);

    expect(decodeVisualIntent("Doric", DORIC_SENSES, serialized)).toEqual(intent);
    expect(decodeVisualIntent("Doric", DORIC_SENSES, "{")).toBeNull();
    expect(decodeVisualIntent(
      "Doric",
      DORIC_SENSES,
      JSON.stringify({ ...intent, extra: true }),
    )).toBeNull();
  });

  it("recognizes common plant and tower language for visual categories", () => {
    expect(validateVisualIntent("fleabane", [{
      partOfSpeech: "noun",
      definition: "A perennial species with daisy-like blossoms.",
      exampleSentence: "Fleabane grew beside the meadow.",
    }], {
      sense_index: 0,
      category: "plant",
      query: "fleabane perennial wildflower",
      description: "A flowering fleabane plant.",
    })).not.toBeNull();
    expect(validateVisualIntent("campanile", [{
      partOfSpeech: "noun",
      definition: "A bell tower standing separately from a church building.",
      exampleSentence: "The medieval campanile dominates the square.",
    }], {
      sense_index: 0,
      category: "architecture",
      query: "campanile bell tower",
      description: "A freestanding campanile bell tower.",
    })).not.toBeNull();
  });
});

describe("Python-compatible vocabulary identity", () => {
  it("performs full Unicode case folding", () => {
    expect(caseFold("Straße")).toBe("strasse");
    expect(caseFold("Σίσυφος")).toBe(caseFold("σίσυφοσ"));
    expect(caseFold("ς")).toBe(caseFold("σ"));
  });

  it("applies NFKC, preserves inner display whitespace, and collapses identity whitespace", () => {
    expect(normalizeEntryText("  Ｐｒｏ\t  Forma  ")).toEqual({
      status: EntryTextStatus.VALID,
      displayText: "Pro\t  Forma",
      normalizedText: "pro forma",
    });
    expect(normalizeEntryText("  re\u0301sume\u0301  ")).toEqual({
      status: EntryTextStatus.VALID,
      displayText: "résumé",
      normalizedText: "résumé",
    });
    expect(normalizeSenseIdentity(" NOUN\t", " same   definition ")).toEqual([
      "noun",
      "same definition",
    ]);
  });

  it("uses Python whitespace semantics for outer trim and identity collapse", () => {
    expect(normalizeEntryText("\u001c Pro\u001dForma \u001f")).toEqual({
      status: EntryTextStatus.VALID,
      displayText: "Pro\u001dForma",
      normalizedText: "pro forma",
    });
  });

  it("enforces the 500-code-point display limit, including astral characters", () => {
    const astral = "🧠";
    expect(MAX_ENTRY_TEXT_LENGTH).toBe(500);
    expect(normalizeEntryText(astral.repeat(500)).status).toBe(EntryTextStatus.VALID);
    expect(normalizeEntryText(astral.repeat(501))).toEqual({
      status: EntryTextStatus.TOO_LONG,
    });
    expect(normalizeEntryText(" \t ")).toEqual({ status: EntryTextStatus.EMPTY });
  });
});

describe("capture parsing and sense validation", () => {
  it("parses the first line as display text and preserves trimmed multiline context", () => {
    expect(parseCaptureMessage("  Pro Forma  \n  Used in finance.\nSecond line.  ")).toEqual({
      displayText: "Pro Forma",
      context: "Used in finance.\nSecond line.",
    });
    expect(parseCaptureMessage("/test")).toBeNull();
    expect(parseCaptureMessage("   ")).toBeNull();
  });

  it("accepts 1 and 20 senses in input order and trims stored fields", () => {
    const one = validateSenseCards([
      { partOfSpeech: " noun ", definition: " meaning ", exampleSentence: " example " },
    ]);
    expect(one).toEqual({
      valid: true,
      cards: [{ partOfSpeech: "noun", definition: "meaning", exampleSentence: "example" }],
    });

    const twenty = validateSenseCards(
      Array.from({ length: MAX_SENSE_COUNT }, (_, index) => ({
        partOfSpeech: "noun",
        definition: `definition ${index}`,
        exampleSentence: `example ${index}`,
      })),
    );
    expect(twenty.valid).toBe(true);
    if (twenty.valid) {
      expect(twenty.cards.map((card) => card.definition)).toEqual(
        Array.from({ length: MAX_SENSE_COUNT }, (_, index) => `definition ${index}`),
      );
    }
  });

  it("rejects zero and 21 senses", () => {
    expect(validateSenseCards([])).toEqual({ valid: false, reason: "count" });
    expect(
      validateSenseCards(
        Array.from({ length: MAX_SENSE_COUNT + 1 }, (_, index) => ({
          partOfSpeech: "noun",
          definition: `definition ${index}`,
          exampleSentence: "example",
        })),
      ),
    ).toEqual({ valid: false, reason: "count" });
  });

  it("enforces code-point boundaries for all sense fields", () => {
    expect(MAX_PART_OF_SPEECH_LENGTH).toBe(50);
    expect(MAX_SENSE_TEXT_LENGTH).toBe(500);
    expect(
      validateSenseCards([
        {
          partOfSpeech: "🧠".repeat(50),
          definition: "🧠".repeat(500),
          exampleSentence: "🧠".repeat(500),
        },
      ]).valid,
    ).toBe(true);

    for (const card of [
      { partOfSpeech: "x".repeat(51), definition: "definition", exampleSentence: "example" },
      { partOfSpeech: "noun", definition: "x".repeat(501), exampleSentence: "example" },
      { partOfSpeech: "noun", definition: "definition", exampleSentence: "x".repeat(501) },
      { partOfSpeech: "", definition: "definition", exampleSentence: "example" },
    ]) {
      expect(validateSenseCards([card])).toEqual({ valid: false, reason: "card" });
    }
  });

  it("rejects duplicate normalized part-of-speech and definition independent of example", () => {
    expect(
      validateSenseCards([
        { partOfSpeech: " noun ", definition: " Same definition ", exampleSentence: "first" },
        { partOfSpeech: "NOUN", definition: "same   definition", exampleSentence: "second" },
      ]),
    ).toEqual({ valid: false, reason: "duplicate" });
  });

  it("validates every card before checking duplicate identities", () => {
    expect(
      validateSenseCards([
        { partOfSpeech: "noun", definition: "same", exampleSentence: "first" },
        { partOfSpeech: "NOUN", definition: "same", exampleSentence: "second" },
        { partOfSpeech: "noun", definition: "valid", exampleSentence: "" },
      ]),
    ).toEqual({ valid: false, reason: "card" });
  });
  it("prepares commands with trimmed context and enforces operation shape", () => {
    expect(MAX_SOURCE_CONTEXT_LENGTH).toBe(2_000);
    expect(
      prepareCaptureCommand({
        displayText: " Pro Forma ",
        operation: CaptureOperation.NEW_ENTRY,
        card: {
          partOfSpeech: " noun ",
          definition: " definition ",
          exampleSentence: " example ",
        },
        sourceContext: ` ${"c".repeat(MAX_SOURCE_CONTEXT_LENGTH)} `,
        matchingSenseId: null,
      }),
    ).toEqual({
      displayText: "Pro Forma",
      normalizedText: "pro forma",
      operation: CaptureOperation.NEW_ENTRY,
      card: {
        partOfSpeech: "noun",
        definition: "definition",
        exampleSentence: "example",
      },
      sourceContext: "c".repeat(MAX_SOURCE_CONTEXT_LENGTH),
      matchingSenseId: null,
    });
    expect(
      prepareCaptureCommand({
        displayText: "bank",
        operation: CaptureOperation.EXISTING_SENSE,
        card: null,
        sourceContext: null,
        matchingSenseId: 4,
      }),
    ).toEqual({
      displayText: "bank",
      normalizedText: "bank",
      operation: CaptureOperation.EXISTING_SENSE,
      card: null,
      sourceContext: null,
      matchingSenseId: 4,
    });
    expect(
      prepareCaptureCommand({
        displayText: "bank",
        operation: CaptureOperation.NEW_ENTRY,
        card: { partOfSpeech: "noun", definition: "definition", exampleSentence: "example" },
        sourceContext: "x".repeat(MAX_SOURCE_CONTEXT_LENGTH + 1),
        matchingSenseId: null,
      }),
    ).toBeNull();
    expect(
      prepareCaptureCommand({
        displayText: "bank",
        operation: CaptureOperation.EXISTING_SENSE,
        card: null,
        sourceContext: null,
        matchingSenseId: null,
      }),
    ).toBeNull();
  });

  it("exports exact serializable values as nominal operation instances", () => {
    expect([
      CaptureOperation.NEW_ENTRY.value,
      CaptureOperation.NEW_SENSE.value,
      CaptureOperation.EXISTING_SENSE.value,
    ]).toEqual(["new_entry", "new_sense", "existing_sense"]);
    expect(CaptureOperation.NEW_ENTRY.toString()).toBe("new_entry");
    expect(CaptureOperation.NEW_ENTRY).not.toBe(CaptureOperation.NEW_SENSE);
  });

  it("rejects raw string operations from untyped input", () => {
    const rawCommand = {
      displayText: "bank",
      operation: "new_entry",
      card: { partOfSpeech: "noun", definition: "definition", exampleSentence: "example" },
      sourceContext: null,
      matchingSenseId: null,
    } as unknown as CaptureCommand;

    expect(prepareCaptureCommand(rawCommand)).toBeNull();
  });

});
