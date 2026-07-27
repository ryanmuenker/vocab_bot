import { DurableObject } from "cloudflare:workers";

import {
  formatDailyReview,
  formatEntry,
  formatHint,
  formatReviewCompletion,
  formatTestCompletion,
  formatTestStart,
} from "./domain/formatting";
import {
  CaptureStatus,
  PendingReviewStatus,
  ReviewCompletionStatus,
  ReviewPromptStatus,
  TestCompletionStatus,
  TestSnapshotStatus,
} from "./domain/models";
import type { VocabularyEntry } from "./domain/models";
import { EntryTextStatus } from "./domain/models";
import { normalizeEntryText } from "./domain/normalization";
import { isHintRequest, parseTestCommand } from "./domain/routing";
import { parseSnapshot, sha256Snapshot } from "./domain/snapshot";
import type { SnapshotSummary, SnapshotV1 } from "./domain/snapshot";
import {
  DefinitionStatus,
  EvaluationStatus,
  OpenCodeAdapter,
} from "./integrations/opencode";
import { splitTelegramMessage, TelegramAdapter } from "./integrations/telegram";
import { initializeSchema } from "./storage/schema";
import { VocabularyStore } from "./storage/vocabulary-store";

const EMPTY_REPLY = "Send a word or phrase.";
const TOO_LONG_REPLY = "Send a word or phrase under 500 characters.";
const NOT_FOUND_REPLY = "I couldn't define that. Please try another word or phrase.";
const DEFINITION_ERROR_REPLY = "I couldn't define that. Please try again.";
const STORAGE_ERROR_REPLY = "I couldn't save that. Please try again.";
const REVIEW_ERROR_REPLY = "I couldn't check your review. Please try again.";
const TEST_ERROR_REPLY = "I couldn't check your test. Please try again.";
const SEND_RETRY_DELAYS_SECONDS = [2, 4, 8, 16, 32, 60, 120, 300, 300, 300] as const;
const ACTIONABLE_ALARM_DELAY_MS = 1_000;

type InboxStatus = "pending" | "waiting" | "ready" | "completed" | "failed";
type InboxKind = "telegram" | "daily_review";

type LanePayload =
  | {
      readonly lane: "capture";
      readonly displayText: string;
      readonly normalizedText: string;
    }
  | {
      readonly lane: "review";
      readonly targetId: number;
      readonly answerText: string;
      readonly entry: VocabularyEntry;
    }
  | {
      readonly lane: "test";
      readonly targetId: number;
      readonly answerText: string;
      readonly entry: VocabularyEntry;
    };

interface InboxRow extends Record<string, SqlStorageValue> {
  id: number;
  dedupe_key: string;
  kind: InboxKind;
  status: InboxStatus;
  payload: string | null;
  prepared_target_id: number | null;
  normalized_key: string | null;
  coalesced_to_event_id: number | null;
  response_text: string | null;
  next_chunk_index: number;
  attempt_count: number;
  created_at: string;
  updated_at: string;
  last_error: string | null;
}

export interface AdmittedTelegramMessage {
  readonly updateId: number;
  readonly messageId: number;
  readonly chatId: string;
  readonly senderId: string;
  readonly text: string;
  readonly receivedAt: string;
}

function timestamp(now = new Date()): string {
  return now.toISOString().replace(/\.000Z$/, "Z");
}

function rows<T extends Record<string, SqlStorageValue>>(cursor: SqlStorageCursor<T>): T[] {
  return Array.from(cursor);
}

export class VocabularyCompanion extends DurableObject<Env> {
  private readonly store: VocabularyStore;
  private readonly provider: OpenCodeAdapter;
  private readonly telegram: TelegramAdapter;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.store = new VocabularyStore(ctx.storage, env.HERMES_TIMEZONE);
    this.provider = new OpenCodeAdapter({
      apiKey: env.OPENCODE_API_KEY,
      baseUrl: env.OPENCODE_BASE_URL,
      model: env.OPENCODE_MODEL,
    });
    this.telegram = new TelegramAdapter({
      botToken: env.TELEGRAM_BOT_TOKEN,
      chatId: env.TELEGRAM_ALLOWED_CHAT_ID,
    });
    void this.ctx.blockConcurrencyWhile(async () => {
      initializeSchema(this.ctx.storage.sql);
    });
  }

  async enqueueTelegramUpdate(
    input: AdmittedTelegramMessage,
  ): Promise<"enqueued" | "duplicate"> {
    const dedupeKey = `telegram:${input.updateId}`;
    if (this.findByDedupeKey(dedupeKey) !== null) {
      await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
      return "duplicate";
    }

    const command = parseTestCommand(input.text);
    if (command === "test" || command === "usage") {
      const response = command === "usage" ? "Usage: /test" : formatTestStart(this.store.startTest(new Date(input.receivedAt)));
      const result = this.insertReadyEvent(dedupeKey, "telegram", response, input.receivedAt);
      await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
      return result;
    }

    const routed = this.ctx.storage.transactionSync(() => {
      if (this.findByDedupeKey(dedupeKey) !== null) return "duplicate" as const;
      const pending = this.store.pendingReview();
      if (pending.status === PendingReviewStatus.STORAGE_ERROR) {
        this.insertEvent(dedupeKey, "telegram", "ready", null, REVIEW_ERROR_REPLY, null, null, input.receivedAt);
        return "enqueued" as const;
      }
      if (pending.status === PendingReviewStatus.PENDING) {
        if (pending.entry === null || pending.event === null) {
          this.insertEvent(dedupeKey, "telegram", "ready", null, REVIEW_ERROR_REPLY, null, null, input.receivedAt);
        } else if (isHintRequest(input.text)) {
          this.insertEvent(dedupeKey, "telegram", "ready", null, formatHint(pending.entry), pending.event.id, null, input.receivedAt);
        } else {
          const payload: LanePayload = {
            lane: "review",
            targetId: pending.event.id,
            answerText: input.text,
            entry: pending.entry,
          };
          this.insertEvent(dedupeKey, "telegram", "pending", payload, null, pending.event.id, null, input.receivedAt);
        }
        return "enqueued" as const;
      }

      const test = this.store.currentTest();
      if (test.status === TestSnapshotStatus.STORAGE_ERROR) {
        this.insertEvent(dedupeKey, "telegram", "ready", null, TEST_ERROR_REPLY, null, null, input.receivedAt);
        return "enqueued" as const;
      }
      if (test.status === TestSnapshotStatus.ACTIVE) {
        const question = test.snapshot?.currentQuestion;
        if (question === null || question === undefined) {
          this.insertEvent(dedupeKey, "telegram", "ready", null, TEST_ERROR_REPLY, null, null, input.receivedAt);
        } else if (isHintRequest(input.text)) {
          this.insertEvent(dedupeKey, "telegram", "ready", null, formatHint(question.entry), question.id, null, input.receivedAt);
        } else {
          const payload: LanePayload = {
            lane: "test",
            targetId: question.id,
            answerText: input.text,
            entry: question.entry,
          };
          this.insertEvent(dedupeKey, "telegram", "pending", payload, null, question.id, null, input.receivedAt);
        }
        return "enqueued" as const;
      }

      const normalized = normalizeEntryText(input.text);
      if (normalized.status === EntryTextStatus.EMPTY) {
        this.insertEvent(dedupeKey, "telegram", "ready", null, EMPTY_REPLY, null, null, input.receivedAt);
        return "enqueued" as const;
      }
      if (normalized.status === EntryTextStatus.TOO_LONG) {
        this.insertEvent(dedupeKey, "telegram", "ready", null, TOO_LONG_REPLY, null, null, input.receivedAt);
        return "enqueued" as const;
      }
      if (normalized.status !== EntryTextStatus.VALID) {
        throw new Error("unreachable entry normalization status");
      }
      const existing = this.store.getEntry(normalized.normalizedText);
      if (existing !== null) {
        this.insertEvent(
          dedupeKey,
          "telegram",
          "ready",
          null,
          formatEntry(existing, "Already saved."),
          existing.id,
          normalized.normalizedText,
          input.receivedAt,
        );
        return "enqueued" as const;
      }
      const leader = rows(
        this.ctx.storage.sql.exec<InboxRow>(
          `SELECT * FROM inbox_events
           WHERE normalized_key = ? AND coalesced_to_event_id IS NULL
             AND status IN ('pending', 'ready')
           ORDER BY id LIMIT 1`,
          normalized.normalizedText,
        ),
      )[0];
      if (leader?.status === "ready") {
        if (leader.response_text === null) throw new Error("ready capture leader has no response");
        this.insertEvent(
          dedupeKey,
          "telegram",
          "ready",
          null,
          leader.response_text,
          leader.prepared_target_id,
          normalized.normalizedText,
          input.receivedAt,
        );
      } else if (leader !== undefined) {
        this.insertEvent(
          dedupeKey,
          "telegram",
          "waiting",
          null,
          null,
          null,
          normalized.normalizedText,
          input.receivedAt,
          leader.id,
        );
      } else {
        const payload: LanePayload = {
          lane: "capture",
          displayText: normalized.displayText,
          normalizedText: normalized.normalizedText,
        };
        this.insertEvent(
          dedupeKey,
          "telegram",
          "pending",
          payload,
          null,
          null,
          normalized.normalizedText,
          input.receivedAt,
        );
      }
      return "enqueued" as const;
    });
    await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
    return routed;
  }

  async enqueueDailyReview(input: {
    readonly dedupeKey: string;
    readonly nowUtc: string;
  }): Promise<"enqueued" | "duplicate"> {
    if (this.findByDedupeKey(input.dedupeKey) !== null) {
      await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
      return "duplicate";
    }
    const response = formatDailyReview(this.store.dailyReview(new Date(input.nowUtc)));
    const result = this.insertReadyEvent(input.dedupeKey, "daily_review", response, input.nowUtc);
    await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
    return result;
  }

  async importSnapshot(snapshot: SnapshotV1, sha256: string): Promise<SnapshotSummary> {
    const parsed = parseSnapshot(snapshot);
    if (parsed === null) throw new TypeError("Invalid SnapshotV1");
    const actualSha256 = await sha256Snapshot(parsed);
    if (actualSha256 !== sha256) throw new TypeError("Snapshot digest mismatch");
    const inboxCount = this.ctx.storage.sql.exec<{ count: number }>(
      "SELECT COUNT(*) AS count FROM inbox_events",
    ).one().count;
    if (inboxCount !== 0) throw new Error("snapshot import requires empty inbox");
    this.store.importSnapshot(parsed);
    return this.snapshotSummary(parsed, actualSha256);
  }

  async exportSnapshot(): Promise<SnapshotV1 | null> {
    const unfinished = this.ctx.storage.sql.exec<{ count: number }>(
      "SELECT COUNT(*) AS count FROM inbox_events WHERE status <> 'completed'",
    ).one().count;
    return unfinished === 0 ? this.store.exportSnapshot() : null;
  }

  async summary(): Promise<SnapshotSummary & { pendingInbox: number; failedInbox: number }> {
    const snapshot = this.store.exportSnapshot();
    const sha256 = await sha256Snapshot(snapshot);
    const counts = this.ctx.storage.sql.exec<{ pending: number; failed: number }>(
      `SELECT
         COALESCE(SUM(CASE WHEN status IN ('pending', 'waiting', 'ready') THEN 1 ELSE 0 END), 0) AS pending,
         COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed
       FROM inbox_events`,
    ).one();
    return {
      ...this.snapshotSummary(snapshot, sha256),
      pendingInbox: counts.pending,
      failedInbox: counts.failed,
    };
  }

  async providerSmoke(displayText: string): Promise<{ status: "found" | "not_found" | "error" }> {
    const result = await this.provider.defineEntry(displayText);
    if (result.status === DefinitionStatus.FOUND) return { status: "found" };
    if (result.status === DefinitionStatus.NOT_FOUND) return { status: "not_found" };
    return { status: "error" };
  }

  async alarm(): Promise<void> {
    const event = rows(
      this.ctx.storage.sql.exec<InboxRow>(
        `SELECT * FROM inbox_events
         WHERE status IN ('pending', 'ready')
         ORDER BY id LIMIT 1`,
      ),
    )[0];
    if (event === undefined) return;
    if (event.status === "pending") await this.prepareEvent(event);
    const retryScheduled = await this.deliverEvent(event.id);
    if (!retryScheduled) await this.rescheduleIfActionable();
  }

  private async prepareEvent(event: InboxRow): Promise<void> {
    if (event.payload === null) {
      this.markFailed(event.id, "missing_payload");
      return;
    }
    let payload: LanePayload;
    try {
      payload = JSON.parse(event.payload) as LanePayload;
    } catch {
      this.markFailed(event.id, "invalid_payload");
      return;
    }

    let response: string;
    if (payload.lane === "capture") {
      const definition = await this.provider.defineEntry(payload.displayText);
      if (definition.status === DefinitionStatus.NOT_FOUND) {
        response = NOT_FOUND_REPLY;
      } else if (definition.status !== DefinitionStatus.FOUND) {
        response = DEFINITION_ERROR_REPLY;
      } else {
        const result = this.store.captureEntry(payload.displayText, definition.cards);
        if (result.status === CaptureStatus.STORAGE_ERROR) response = STORAGE_ERROR_REPLY;
        else if (result.entry === null || result.status === CaptureStatus.INVALID) response = DEFINITION_ERROR_REPLY;
        else response = formatEntry(
          result.entry,
          result.status === CaptureStatus.SAVED ? "✓ Saved." : "Already saved.",
        );
      }
      this.ctx.storage.transactionSync(() => {
        this.readyEvent(event.id, response);
        const followers = rows(
          this.ctx.storage.sql.exec<{ id: number }>(
            "SELECT id FROM inbox_events WHERE status = 'waiting' AND coalesced_to_event_id = ? ORDER BY id",
            event.id,
          ),
        );
        for (const follower of followers) this.readyEvent(follower.id, response);
      });
      return;
    }

    const evaluation = await this.provider.evaluateAnswer(payload.entry, payload.answerText);
    if (evaluation.status !== EvaluationStatus.VALID || evaluation.evaluation === null) {
      this.readyEvent(
        event.id,
        payload.lane === "review"
          ? formatReviewCompletion({ status: ReviewCompletionStatus.STORAGE_ERROR })
          : formatTestCompletion({ status: TestCompletionStatus.STORAGE_ERROR }),
      );
      return;
    }
    let persisted = false;
    if (payload.lane === "review") {
      const result = this.store.completeReview(
        payload.targetId,
        payload.answerText,
        evaluation.evaluation,
        new Date(),
        (completed) => {
          this.readyEvent(event.id, formatReviewCompletion(completed));
          persisted = true;
        },
      );
      if (!persisted) this.readyEvent(event.id, formatReviewCompletion(result));
    } else {
      const result = this.store.completeTest(
        payload.targetId,
        payload.answerText,
        evaluation.evaluation,
        new Date(),
        (completed) => {
          this.readyEvent(event.id, formatTestCompletion(completed));
          persisted = true;
        },
      );
      if (!persisted) this.readyEvent(event.id, formatTestCompletion(result));
    }
  }

  private async deliverEvent(eventId: number): Promise<boolean> {
    const event = this.inboxById(eventId);
    if (event === null || event.status !== "ready" || event.response_text === null) return false;
    const chunks = splitTelegramMessage(event.response_text);
    if (event.response_text === "" || event.next_chunk_index >= chunks.length) {
      this.completeDelivery(event.id);
      return false;
    }
    try {
      await this.telegram.sendText(chunks[event.next_chunk_index]!);
      const next = event.next_chunk_index + 1;
      if (next >= chunks.length) this.completeDelivery(event.id);
      else {
        const now = timestamp();
        rows(this.ctx.storage.sql.exec(
          "UPDATE inbox_events SET next_chunk_index = ?, attempt_count = 0, updated_at = ?, last_error = NULL WHERE id = ? AND status = 'ready'",
          next,
          now,
          event.id,
        ));
        await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
      }
      return false;
    } catch {
      const attempts = event.attempt_count + 1;
      const now = timestamp();
      if (attempts >= SEND_RETRY_DELAYS_SECONDS.length) {
        rows(this.ctx.storage.sql.exec(
          `UPDATE inbox_events
           SET status = 'failed', attempt_count = ?, updated_at = ?, last_error = 'telegram_delivery_failed', payload = NULL
           WHERE id = ? AND status = 'ready'`,
          attempts,
          now,
          event.id,
        ));
        console.error(JSON.stringify({
          event: "inbox_delivery_failed",
          eventId: event.id,
          kind: event.kind,
          attemptCount: attempts,
        }));
      } else {
        rows(this.ctx.storage.sql.exec(
          `UPDATE inbox_events
           SET attempt_count = ?, updated_at = ?, last_error = 'telegram_delivery_failed'
           WHERE id = ? AND status = 'ready'`,
          attempts,
          now,
          event.id,
        ));
        await this.ctx.storage.setAlarm(
          Date.now() + SEND_RETRY_DELAYS_SECONDS[attempts - 1]! * 1_000,
        );
        return true;
      }
    }
    return false;
  }

  private insertReadyEvent(
    dedupeKey: string,
    kind: InboxKind,
    response: string,
    createdAt: string,
  ): "enqueued" | "duplicate" {
    return this.ctx.storage.transactionSync(() => {
      if (this.findByDedupeKey(dedupeKey) !== null) return "duplicate";
      this.insertEvent(dedupeKey, kind, "ready", null, response, null, null, createdAt);
      return "enqueued";
    });
  }

  private insertEvent(
    dedupeKey: string,
    kind: InboxKind,
    status: InboxStatus,
    payload: LanePayload | null,
    responseText: string | null,
    preparedTargetId: number | null,
    normalizedKey: string | null,
    createdAt: string,
    coalescedToEventId: number | null = null,
  ): void {
    rows(this.ctx.storage.sql.exec(
      `INSERT INTO inbox_events
        (dedupe_key, kind, status, payload, prepared_target_id, normalized_key,
         coalesced_to_event_id, response_text, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      dedupeKey,
      kind,
      status,
      payload === null ? null : JSON.stringify(payload),
      preparedTargetId,
      normalizedKey,
      coalescedToEventId,
      responseText,
      createdAt,
      createdAt,
    ));
  }

  private readyEvent(id: number, response: string): void {
    const now = timestamp();
    rows(this.ctx.storage.sql.exec(
      `UPDATE inbox_events
       SET status = 'ready', response_text = ?, payload = NULL, next_chunk_index = 0,
           attempt_count = 0, updated_at = ?, last_error = NULL
       WHERE id = ? AND status IN ('pending', 'waiting')`,
      response,
      now,
      id,
    ));
  }

  private completeDelivery(id: number): void {
    rows(this.ctx.storage.sql.exec(
      `UPDATE inbox_events
       SET status = 'completed', payload = NULL, response_text = NULL,
           updated_at = ?, last_error = NULL
       WHERE id = ? AND status = 'ready'`,
      timestamp(),
      id,
    ));
  }

  private markFailed(id: number, reason: string): void {
    rows(this.ctx.storage.sql.exec(
      `UPDATE inbox_events
       SET status = 'failed', payload = NULL, response_text = NULL, updated_at = ?, last_error = ?
       WHERE id = ?`,
      timestamp(),
      reason,
      id,
    ));
  }

  private inboxById(id: number): InboxRow | null {
    return rows(this.ctx.storage.sql.exec<InboxRow>("SELECT * FROM inbox_events WHERE id = ?", id))[0] ?? null;
  }

  private findByDedupeKey(key: string): InboxRow | null {
    return rows(
      this.ctx.storage.sql.exec<InboxRow>("SELECT * FROM inbox_events WHERE dedupe_key = ?", key),
    )[0] ?? null;
  }

  private async rescheduleIfActionable(): Promise<void> {
    const actionable = this.ctx.storage.sql.exec<{ count: number }>(
      "SELECT COUNT(*) AS count FROM inbox_events WHERE status IN ('pending', 'ready')",
    ).one().count;
    if (actionable > 0) {
      await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
    }
  }

  private snapshotSummary(snapshot: SnapshotV1, sha256: string): SnapshotSummary {
    return {
      entries: snapshot.entries.length,
      senses: snapshot.senses.length,
      reviewEvents: snapshot.reviewEvents.length,
      testSessions: snapshot.testSessions.length,
      testQuestions: snapshot.testQuestions.length,
      sha256,
    };
  }
}
