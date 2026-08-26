import { env, exports } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import type { SnapshotV2 } from "../src/domain/snapshot";

/** A mis-entered entry plus a second entry whose normalized text can collide. */
const SNAPSHOT: SnapshotV2 = {
  formatVersion: 2,
  entries: [
    {
      id: 45,
      displayText: "Abscond\nAbscond",
      normalizedText: "abscond abscond",
      dateAdded: "2026-07-19T12:45:48.055085Z",
      lastReviewed: null,
      reviewStatus: "new",
    },
    {
      id: 46,
      displayText: "Harbour",
      normalizedText: "harbour",
      dateAdded: "2026-07-20T00:00:00Z",
      lastReviewed: null,
      reviewStatus: "new",
    },
  ],
  senses: [],
  reviewEvents: [],
  testSessions: [],
  testQuestions: [],
  cards: [],
  studySessions: [],
  studyQueue: [],
  studyPrompts: [],
  deliveryAttempts: [],
  answerDrafts: [],
  reviewAttempts: [],
};

function fixEntry(body: unknown) {
  return exports.default.fetch(new Request("https://example.test/admin/fix-entry", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer test-admin-token",
    },
    body: JSON.stringify(body),
  }));
}

function deleteEntries(body: unknown) {
  return exports.default.fetch(new Request("https://example.test/admin/delete-entries", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer test-admin-token",
    },
    body: JSON.stringify(body),
  }));
}

describe("admin fix-entry surface", () => {
  it("requires admin auth and rejects non-JSON bodies", async () => {
    expect(
      (await exports.default.fetch(new Request("https://example.test/admin/fix-entry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: 45, displayText: "Abscond" }),
      }))).status,
    ).toBe(401);
    // Malformed JSON on admin routes follows the existing convention: 500.
    expect(
      (await exports.default.fetch(new Request("https://example.test/admin/fix-entry", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer test-admin-token" },
        body: "not json",
      }))).status,
    ).toBe(500);
  });

  it("rejects bad input, then corrects a mis-entered entry end to end", async () => {
    const stub = env.VOCABULARY.getByName("123456");
    await stub.importSnapshot(SNAPSHOT);

    for (const body of [
      { id: 45 },
      { displayText: "Abscond" },
      { id: 0, displayText: "Abscond" },
      { id: "45", displayText: "Abscond" },
      { id: 45, displayText: "   " },
      { id: 45, displayText: "Abscond", extra: true },
      { id: 45, displayText: "x".repeat(501) },
    ]) {
      expect((await fixEntry(body)).status).toBe(400);
    }

    const missing = await fixEntry({ id: 999, displayText: "Abscond" });
    expect(missing.status).toBe(404);
    expect(await missing.json()).toEqual({ error: "entry not found" });

    // "harbour" normalizes to entry 46's normalized text, which is taken.
    const colliding = await fixEntry({ id: 45, displayText: "harbour" });
    expect(colliding.status).toBe(409);
    expect(await colliding.json()).toEqual({ error: "another entry already uses that text" });

    const response = await fixEntry({ id: 45, displayText: "Abscond" });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      status: "updated",
      displayText: "Abscond",
      normalizedText: "abscond",
    });

    const exported = (await stub.exportSnapshot())!;
    expect(exported.entries).toEqual([
      expect.objectContaining({ id: 45, displayText: "Abscond", normalizedText: "abscond" }),
      expect.objectContaining({ id: 46, displayText: "Harbour", normalizedText: "harbour" }),
    ]);
  });
});

describe("admin delete-entries surface", () => {
  it("requires admin auth and rejects invalid batches", async () => {
    expect(
      (await exports.default.fetch(new Request("https://example.test/admin/delete-entries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ displayTexts: ["Abscond"] }),
      }))).status,
    ).toBe(401);

    for (const body of [
      {},
      { displayTexts: [] },
      { displayTexts: ["   "] },
      { displayTexts: [45] },
      { displayTexts: Array.from({ length: 101 }, () => "word") },
      { displayTexts: ["Abscond"], extra: true },
    ]) {
      expect((await deleteEntries(body)).status).toBe(400);
    }
  });

  it("deletes exact normalized entries and reports misses", async () => {
    const stub = env.VOCABULARY.getByName("123456");
    await deleteEntries({ displayTexts: ["Abscond", "Abscond\nAbscond", "Harbour"] });
    await stub.importSnapshot(SNAPSHOT);

    const response = await deleteEntries({
      displayTexts: ["  ABSCOND\nABSCOND  ", "missing", "MISSING"],
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      deleted: [{
        id: 45,
        displayText: "Abscond\nAbscond",
        normalizedText: "abscond abscond",
      }],
      notFound: ["missing"],
    });
    expect((await stub.exportSnapshot())!.entries).toEqual([
      expect.objectContaining({ id: 46, displayText: "Harbour", normalizedText: "harbour" }),
    ]);
  });
});
