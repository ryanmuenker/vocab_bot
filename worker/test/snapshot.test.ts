import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import {
  canonicalizeJcs,
  parseSnapshot,
  sha256Snapshot,
  type SnapshotV1,
} from "../src/domain/snapshot";
import { VocabularyStore } from "../src/storage/vocabulary-store";

const SNAPSHOT: SnapshotV1 = {
  formatVersion: 1,
  entries: [{
    id: 7,
    displayText: "Straße 😀",
    normalizedText: "strasse 😀",
    dateAdded: "2026-07-20T00:00:00Z",
    lastReviewed: null,
    reviewStatus: "new",
  }],
  senses: [{
    id: 11,
    entryId: 7,
    definition: "café \"quote\" \\ slash\u2028line",
    partOfSpeech: "noun",
    exampleSentence: "Control-safe example.",
    sourceContext: "source",
    dateAdded: "2026-07-20T00:00:00Z",
  }],
  reviewEvents: [{
    id: 3,
    entryId: 7,
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

describe("SnapshotV1 and JCS", () => {
  it("matches the cross-language constrained JCS vector", async () => {
    const vector = {
      nested: { z: null, a: true },
      id: 9_007_199_254_740_991,
      array: ["café", "Straße", "😀", "\u2028", "\"", "\\", "\u0000\b\t\n\f\r\u001f"],
    };
    expect(canonicalizeJcs(vector)).toBe(
      "{\"array\":[\"café\",\"Straße\",\"😀\",\" \",\"\\\"\",\"\\\\\",\"\\u0000\\b\\t\\n\\f\\r\\u001f\"],\"id\":9007199254740991,\"nested\":{\"a\":true,\"z\":null}}",
    );
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(canonicalizeJcs(vector)),
    );
    expect(Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""))
      .toBe("f9aff60e240798e6b07e32bcfd2d38464a36e899642f167589c4a660752a61f6");
    expect(() => canonicalizeJcs("\ud800")).toThrow(/lone surrogate/u);
  });

  it("validates, imports, and exports explicit IDs and pending state without recomputing identity", async () => {
    expect(parseSnapshot(SNAPSHOT)).toEqual(SNAPSHOT);
    const expectedHash = await sha256Snapshot(SNAPSHOT);
    await runInDurableObject(
      env.VOCABULARY.getByName(`snapshot-${crypto.randomUUID()}`),
      (_instance, state) => {
        const store = new VocabularyStore(state.storage);
        store.importSnapshot(SNAPSHOT);
        expect(store.exportSnapshot()).toEqual(SNAPSHOT);
        expect(store.getEntry("STRASSE 😀")?.normalizedText).toBe("strasse 😀");
        expect(() => store.importSnapshot(SNAPSHOT)).toThrow(/empty storage/u);
      },
    );
    expect(expectedHash).toMatch(/^[0-9a-f]{64}$/u);
  });
});
