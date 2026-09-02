import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import {
  CaptureStatus,
  EntryImageOrigin,
  ImageBackfillStatus,
  VisualCategory,
} from "../src/domain/models";
import type { SenseCard, VisualIntent, VocabularyEntry } from "../src/domain/models";
import { initializeSchema } from "../src/storage/schema";
import { VocabularyStore } from "../src/storage/vocabulary-store";

const CARD: SenseCard = {
  partOfSpeech: "noun",
  definition: "a visible object",
  exampleSentence: "The object is visible.",
};

const INTENT: VisualIntent = {
  senseIndex: 0,
  category: VisualCategory.OBJECT,
  query: "red ceramic teapot",
  description: "a red ceramic teapot on a plain background",
};

function stub() {
  return env.VOCABULARY.getByName(`image-storage-${crypto.randomUUID()}`);
}

function rows<T extends Record<string, SqlStorageValue>>(
  storage: DurableObjectStorage,
  query: string,
  ...bindings: SqlStorageValue[]
): T[] {
  return Array.from(storage.sql.exec<T>(query, ...bindings));
}

function capture(store: VocabularyStore, text: string, day: number): VocabularyEntry {
  const result = store.captureEntry(
    text,
    [CARD],
    new Date(`2026-08-${String(day).padStart(2, "0")}T00:00:00Z`),
  );
  expect(result.status).toBe(CaptureStatus.SAVED);
  if (result.entry === null) throw new Error(`failed to capture ${text}`);
  return result.entry;
}

describe("entry image schema", () => {
  it("creates the image tables and enforces ownership and one association per entry", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      initializeSchema(state.storage.sql);
      const store = new VocabularyStore(state.storage);
      const first = capture(store, "teapot", 1);
      const second = capture(store, "lantern", 2);

      expect(rows<{ name: string }>(
        state.storage,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('vocabulary_entry_images', 'image_backfill_attempts') ORDER BY name",
      ).map(({ name }) => name)).toEqual([
        "image_backfill_attempts",
        "vocabulary_entry_images",
      ]);

      expect(() => rows(
        state.storage,
        `INSERT INTO vocabulary_entry_images
          (entry_id, sense_id, category, query, description, origin, created_at, updated_at)
         VALUES (?, ?, 'object', 'teapot', 'a teapot', 'capture', ?, ?)`,
        first.id,
        second.senses[0].id,
        "2026-08-03T00:00:00Z",
        "2026-08-03T00:00:00Z",
      )).toThrow();

      rows(
        state.storage,
        `INSERT INTO vocabulary_entry_images
          (entry_id, sense_id, category, query, description, origin, created_at, updated_at)
         VALUES (?, ?, 'object', 'teapot', 'a teapot', 'capture', ?, ?)`,
        first.id,
        first.senses[0].id,
        "2026-08-03T00:00:00Z",
        "2026-08-03T00:00:00Z",
      );
      expect(() => rows(
        state.storage,
        `INSERT INTO vocabulary_entry_images
          (entry_id, sense_id, category, query, description, origin, created_at, updated_at)
         VALUES (?, ?, 'object', 'second', 'another image', 'backfill', ?, ?)`,
        first.id,
        first.senses[0].id,
        "2026-08-04T00:00:00Z",
        "2026-08-04T00:00:00Z",
      )).toThrow();
      expect(rows<{ count: number }>(
        state.storage,
        "SELECT COUNT(*) AS count FROM vocabulary_entry_images WHERE entry_id = ?",
        first.id,
      )[0].count).toBe(1);
    });
  });

  it("requires complete candidate metadata and a paired receipt backed by a candidate", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      initializeSchema(state.storage.sql);
      const store = new VocabularyStore(state.storage);
      const entry = capture(store, "teapot", 1);
      store.saveEntryImageIntent(
        entry.id,
        entry.senses[0].id,
        INTENT,
        EntryImageOrigin.CAPTURE,
      );

      expect(() => rows(
        state.storage,
        "UPDATE vocabulary_entry_images SET photo_url = ? WHERE entry_id = ?",
        "https://upload.wikimedia.org/teapot.jpg",
        entry.id,
      )).toThrow();
      expect(() => rows(
        state.storage,
        "UPDATE vocabulary_entry_images SET caption = ?, source_url = ? WHERE entry_id = ?",
        "A teapot",
        "https://commons.wikimedia.org/wiki/File:Teapot.jpg",
        entry.id,
      )).toThrow();
      expect(() => rows(
        state.storage,
        "UPDATE vocabulary_entry_images SET telegram_file_id = ?, telegram_file_unique_id = ? WHERE entry_id = ?",
        "telegram-file",
        "telegram-unique",
        entry.id,
      )).toThrow();

      rows(
        state.storage,
        "UPDATE vocabulary_entry_images SET photo_url = ?, caption = ?, source_url = ? WHERE entry_id = ?",
        "https://upload.wikimedia.org/teapot.jpg",
        "A red teapot",
        "https://commons.wikimedia.org/wiki/File:Teapot.jpg",
        entry.id,
      );
      expect(() => rows(
        state.storage,
        "UPDATE vocabulary_entry_images SET telegram_file_id = ? WHERE entry_id = ?",
        "telegram-file",
        entry.id,
      )).toThrow();
      rows(
        state.storage,
        "UPDATE vocabulary_entry_images SET telegram_file_id = ?, telegram_file_unique_id = ? WHERE entry_id = ?",
        "telegram-file",
        "telegram-unique",
        entry.id,
      );

      expect(rows<{
        photo_url: string;
        caption: string;
        source_url: string;
        telegram_file_id: string;
        telegram_file_unique_id: string;
      }>(
        state.storage,
        `SELECT photo_url, caption, source_url, telegram_file_id, telegram_file_unique_id
         FROM vocabulary_entry_images WHERE entry_id = ?`,
        entry.id,
      )).toEqual([{
        photo_url: "https://upload.wikimedia.org/teapot.jpg",
        caption: "A red teapot",
        source_url: "https://commons.wikimedia.org/wiki/File:Teapot.jpg",
        telegram_file_id: "telegram-file",
        telegram_file_unique_id: "telegram-unique",
      }]);
    });
  });

  it("bounds retry ledger statuses and their error payloads", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      initializeSchema(state.storage.sql);
      const store = new VocabularyStore(state.storage);
      const entry = capture(store, "teapot", 1);
      const insertAttempt = `
        INSERT INTO image_backfill_attempts
          (entry_id, status, attempt_count, last_error, attempted_at)
        VALUES (?, ?, 1, ?, '2026-08-03T00:00:00Z')`;

      expect(() => rows(
        state.storage,
        insertAttempt,
        entry.id,
        "unknown",
        "bad status",
      )).toThrow();
      expect(() => rows(
        state.storage,
        insertAttempt,
        entry.id,
        ImageBackfillStatus.NO_VISUAL,
        "unexpected error",
      )).toThrow();
      expect(() => rows(
        state.storage,
        insertAttempt,
        entry.id,
        ImageBackfillStatus.PROVIDER_ERROR,
        null,
      )).toThrow();

      rows(
        state.storage,
        insertAttempt,
        entry.id,
        ImageBackfillStatus.NO_VISUAL,
        null,
      );
      expect(() => rows(
        state.storage,
        insertAttempt,
        entry.id,
        ImageBackfillStatus.NO_VISUAL,
        null,
      )).toThrow();
      expect(rows<{
        status: string;
        attempt_count: number;
        last_error: string | null;
      }>(
        state.storage,
        "SELECT status, attempt_count, last_error FROM image_backfill_attempts",
      )).toEqual([{
        status: ImageBackfillStatus.NO_VISUAL,
        attempt_count: 1,
        last_error: null,
      }]);
    });
  });

  it("cascades image associations and retry attempts with their entry", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      initializeSchema(state.storage.sql);
      const store = new VocabularyStore(state.storage);
      const pictured = capture(store, "teapot", 1);
      const failed = capture(store, "lantern", 2);
      store.saveEntryImageIntent(
        pictured.id,
        pictured.senses[0].id,
        INTENT,
        EntryImageOrigin.CAPTURE,
      );
      store.recordImageBackfillAttempt(
        failed.id,
        ImageBackfillStatus.IMAGE_UNAVAILABLE,
        "no suitable photo",
      );

      expect(store.deleteEntries([pictured.normalizedText, failed.normalizedText]).deleted).toHaveLength(2);
      expect(rows<{ count: number }>(
        state.storage,
        "SELECT COUNT(*) AS count FROM vocabulary_entry_images",
      )[0].count).toBe(0);
      expect(rows<{ count: number }>(
        state.storage,
        "SELECT COUNT(*) AS count FROM image_backfill_attempts",
      )[0].count).toBe(0);
    });
  });
});

describe("VocabularyStore entry images", () => {
  it("persists the intent, candidate, and reusable Telegram receipt transitions", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      initializeSchema(state.storage.sql);
      const store = new VocabularyStore(state.storage);
      const entry = capture(store, "teapot", 1);
      const createdAt = new Date("2026-08-02T10:00:00Z");

      expect(store.getEntryById(entry.id)).toEqual(entry);
      expect(store.getEntryById(0)).toBeNull();
      expect(store.getEntryImage(entry.id)).toBeNull();
      expect(store.attachEntryImageReceipt(entry.id, {
        telegramFileId: "premature-file",
        telegramFileUniqueId: "premature-unique",
      })).toBe(false);

      const intent = store.saveEntryImageIntent(
        entry.id,
        entry.senses[0].id,
        INTENT,
        EntryImageOrigin.CAPTURE,
        createdAt,
      );
      expect(intent).toMatchObject({
        entryId: entry.id,
        senseId: entry.senses[0].id,
        category: VisualCategory.OBJECT,
        query: INTENT.query,
        description: INTENT.description,
        photoUrl: null,
        caption: null,
        sourceUrl: null,
        telegramFileId: null,
        telegramFileUniqueId: null,
        origin: EntryImageOrigin.CAPTURE,
        createdAt: "2026-08-02T10:00:00Z",
        updatedAt: "2026-08-02T10:00:00Z",
      });
      expect(store.saveEntryImageIntent(
        entry.id,
        entry.senses[0].id,
        { ...INTENT, query: "replacement query" },
        EntryImageOrigin.BACKFILL,
      )).toEqual(intent);

      expect(store.saveEntryImageCandidate(entry.id, {
        photoUrl: "https://upload.wikimedia.org/teapot.jpg",
        caption: "A red ceramic teapot",
        sourceUrl: "https://commons.wikimedia.org/wiki/File:Teapot.jpg",
      }, new Date("2026-08-02T10:01:00Z"))).toBe(true);
      expect(store.saveEntryImageCandidate(entry.id, {
        photoUrl: "https://upload.wikimedia.org/other.jpg",
        caption: "A replacement",
        sourceUrl: "https://commons.wikimedia.org/wiki/File:Other.jpg",
      })).toBe(false);

      expect(store.attachEntryImageReceipt(entry.id, {
        telegramFileId: "telegram-file",
        telegramFileUniqueId: "telegram-unique",
      }, new Date("2026-08-02T10:02:00Z"))).toBe(true);
      expect(store.attachEntryImageReceipt(entry.id, {
        telegramFileId: "replacement-file",
        telegramFileUniqueId: "replacement-unique",
      })).toBe(false);
      expect(store.getEntryImage(entry.id)).toMatchObject({
        photoUrl: "https://upload.wikimedia.org/teapot.jpg",
        caption: "A red ceramic teapot",
        sourceUrl: "https://commons.wikimedia.org/wiki/File:Teapot.jpg",
        telegramFileId: "telegram-file",
        telegramFileUniqueId: "telegram-unique",
        updatedAt: "2026-08-02T10:02:00Z",
      });
    });
  });

  it("selects bounded backfill work and maintains retry ledger summary state", async () => {
    await runInDurableObject(stub(), (_instance, state) => {
      initializeSchema(state.storage.sql);
      const store = new VocabularyStore(state.storage);
      const entries = [
        capture(store, "entry-one", 1),
        capture(store, "entry-two", 2),
        capture(store, "entry-three", 3),
        capture(store, "entry-four", 4),
      ];

      expect(store.imageBackfillEntries(2, false).map(({ id }) => id)).toEqual([
        entries[0].id,
        entries[1].id,
      ]);
      store.recordImageBackfillAttempt(
        entries[0].id,
        ImageBackfillStatus.NO_VISUAL,
        null,
        new Date("2026-08-05T00:00:00Z"),
      );
      store.recordImageBackfillAttempt(
        entries[1].id,
        ImageBackfillStatus.PROVIDER_ERROR,
        "provider timed out",
        new Date("2026-08-05T00:01:00Z"),
      );
      store.saveEntryImageIntent(
        entries[2].id,
        entries[2].senses[0].id,
        INTENT,
        EntryImageOrigin.BACKFILL,
      );

      expect(store.imageBackfillEntries(10, false).map(({ id }) => id)).toEqual([
        entries[3].id,
      ]);
      expect(store.imageBackfillEntries(2, true).map(({ id }) => id)).toEqual([
        entries[0].id,
        entries[1].id,
      ]);
      expect(store.imageBackfillSummary()).toEqual({
        totalEntries: 4,
        associatedEntries: 1,
        neverAttemptedEntries: 1,
        attempts: {
          no_visual: 1,
          provider_error: 1,
          rate_limited: 0,
          invalid_response: 0,
          image_unavailable: 0,
        },
      });

      store.recordImageBackfillAttempt(
        entries[1].id,
        ImageBackfillStatus.RATE_LIMITED,
        "retry after 60 seconds",
        new Date("2026-08-05T00:02:00Z"),
      );
      expect(rows<{
        status: string;
        attempt_count: number;
        last_error: string;
        attempted_at: string;
      }>(
        state.storage,
        "SELECT status, attempt_count, last_error, attempted_at FROM image_backfill_attempts WHERE entry_id = ?",
        entries[1].id,
      )).toEqual([{
        status: ImageBackfillStatus.RATE_LIMITED,
        attempt_count: 2,
        last_error: "retry after 60 seconds",
        attempted_at: "2026-08-05T00:02:00Z",
      }]);

      store.saveEntryImageIntent(
        entries[0].id,
        entries[0].senses[0].id,
        INTENT,
        EntryImageOrigin.BACKFILL,
      );
      store.recordImageBackfillAttempt(
        entries[2].id,
        ImageBackfillStatus.IMAGE_UNAVAILABLE,
        "must not overwrite an association",
      );
      expect(store.imageBackfillSummary()).toEqual({
        totalEntries: 4,
        associatedEntries: 2,
        neverAttemptedEntries: 1,
        attempts: {
          no_visual: 0,
          provider_error: 0,
          rate_limited: 1,
          invalid_response: 0,
          image_unavailable: 0,
        },
      });
    });
  });
});
