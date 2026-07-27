import { DurableObject } from "cloudflare:workers";

import {
  formatDirectionalTotals,
  formatEntry,
  formatHint,
  formatStudyEvaluation,
  formatStudyEvaluationResult,
  formatStudyPrompt,
  formatStudySchedule,
} from "./domain/formatting";
import {
  CaptureStatus,
  CardDirection,
  EntryTextStatus,
  EvaluationGrade,
  FinalizeStatus,
  StudyMode,
  StudyMutationStatus,
  StudyPromptStatus,
  StudySessionStatus,
  StudyStartStatus,
} from "./domain/models";
import type {
  Evaluation,
  ReviewRating,
  StudyAnswerContext,
  StudyPromptSnapshot,
} from "./domain/models";
import { caseFold, normalizeEntryText, trimPythonWhitespace } from "./domain/normalization";
import {
  allowedRatings,
  isHintRequest,
  normalizeReverseAnswer,
  parseRating,
  parseStudyCommand,
} from "./domain/routing";
import type { StudyCommand } from "./domain/routing";
import { parseSnapshot, readSnapshot, sha256Snapshot, summarizeSnapshot, writeSnapshot } from "./domain/snapshot";
import type { SnapshotSummary, SnapshotV2 } from "./domain/snapshot";
import {
  DefinitionStatus,
  EvaluationStatus,
  OpenCodeAdapter,
} from "./integrations/opencode";
import { splitTelegramMessage, TelegramAdapter } from "./integrations/telegram";
import { initializeSchema } from "./storage/schema";
import { TEST_REQUIRED_CARDS, VocabularyStore } from "./storage/vocabulary-store";

const EMPTY_REPLY = "Send a word or phrase.";
const TOO_LONG_REPLY = "Send a word or phrase under 500 characters.";
const NOT_FOUND_REPLY = "I couldn't define that. Please try another word or phrase.";
const DEFINITION_ERROR_REPLY = "I couldn't define that. Please try again.";
const STORAGE_ERROR_REPLY = "I couldn't save that. Please try again.";
const REVIEW_ERROR_REPLY = "I couldn't load that review. Please try again.";
const STUDY_STORAGE_ERROR_REPLY = "I couldn't save that study step. Please try again.";
const NO_ACTIVE_REPLY = "There isn't a delivered study prompt waiting.";
const STALE_PROMPT_REPLY = "That study prompt is no longer current.";
const INVALID_ANSWER_REPLY = "Send a non-empty answer.";
const INVALID_RATING_REPLY = "Send one of the listed effort ratings.";
// The prompt stays answerable after a failed evaluation, so the next message
// is graded as the answer. Say so: a learner who replies with a question
// instead of re-sending their answer gets that question graded.
const EVALUATION_ERROR_REPLY =
  "I couldn't evaluate that answer, and nothing was recorded. " +
  "Send your answer again — the next message you send is graded as your answer.";
const REVIEW_USAGE = "Usage: /review";
const ENDSTUDY_USAGE = "Usage: /endstudy";
const TEST_USAGE =
  "Usage: /test forward|reverse\n" +
  "Forward: recall each saved meaning from its word.\n" +
  "Reverse: recall the saved word from one exact definition.";
const SHOW_ANSWER = "show answer";
const IDK_FEEDBACK = "You said you don't know the answer.";
const REVERSE_CORRECT_FEEDBACK = "Exact match to the saved entry.";
const REVERSE_INCORRECT_FEEDBACK = "That does not exactly match the saved entry.";
const DEFAULT_REVIEW_HOUR = 12;
const SEND_RETRY_DELAYS_SECONDS = [2, 4, 8, 16, 32, 60, 120, 300, 300, 300] as const;
const ACTIONABLE_ALARM_DELAY_MS = 1_000;

type InboxStatus = "pending" | "waiting" | "ready" | "completed" | "failed";
type InboxKind = "telegram" | "daily_review";
type Routed = "enqueued" | "duplicate";

type LanePayload =
  | {
      readonly lane: "capture";
      readonly displayText: string;
      readonly normalizedText: string;
    }
  | {
      readonly lane: "study";
      readonly promptId: number;
      readonly answerText: string;
    };

interface InboxRow extends Record<string, SqlStorageValue> {
  id: number;
  dedupe_key: string;
  kind: InboxKind;
  status: InboxStatus;
  /**
   * Lane input while pending; once ready it carries the Telegram message ids of
   * the chunks already sent, so the delivery attempt can record every one.
   */
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
  return now.toISOString().replace(/\.000Z$/u, "Z");
}

function rows<T extends Record<string, SqlStorageValue>>(cursor: SqlStorageCursor<T>): T[] {
  return Array.from(cursor);
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * The outstanding question replayed verbatim alongside the message that could
 * not be an answer, so nothing the learner sent is graded, captured, or lost.
 */
function interruption(label: string, promptText: string, userText: string, exit: string): string {
  return (
    `${label} Answer this delivered question first:\n\n` +
    `${promptText}\n\n` +
    "Your original message was:\n" +
    `${userText}\n\n` +
    `Complete or exit the ${exit}, then resubmit it.`
  );
}

export class VocabularyCompanion extends DurableObject<Env> {
  private readonly store: VocabularyStore;
  private readonly provider: OpenCodeAdapter;
  private readonly telegram: TelegramAdapter;
  private readonly reviewHour: number;
  private readonly timeZone: string;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // Keep the default in step with VocabularyStore's, so a missing
    // HERMES_TIMEZONE renders due times in the same zone the scheduler uses.
    this.timeZone = env.HERMES_TIMEZONE || "Asia/Kuala_Lumpur";
    this.store = new VocabularyStore(ctx.storage, this.timeZone);
    this.provider = new OpenCodeAdapter({
      apiKey: env.OPENCODE_API_KEY,
      baseUrl: env.OPENCODE_BASE_URL,
      model: env.OPENCODE_MODEL,
    });
    this.telegram = new TelegramAdapter({
      botToken: env.TELEGRAM_BOT_TOKEN,
      chatId: env.TELEGRAM_ALLOWED_CHAT_ID,
    });
    const hour = Number(env.HERMES_REVIEW_HOUR);
    this.reviewHour =
      Number.isSafeInteger(hour) && hour >= 0 && hour <= 23 ? hour : DEFAULT_REVIEW_HOUR;
    void this.ctx.blockConcurrencyWhile(async () => {
      initializeSchema(this.ctx.storage.sql);
    });
  }

  async enqueueTelegramUpdate(input: AdmittedTelegramMessage): Promise<Routed> {
    const dedupeKey = `telegram:${input.updateId}`;
    if (this.findByDedupeKey(dedupeKey) !== null) {
      await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
      return "duplicate";
    }
    const now = new Date(input.receivedAt);
    const command = parseStudyCommand(input.text);
    // Routing is synchronous, so the dedupe check and the insert cannot
    // interleave with another update on this object.
    const routed =
      command === null
        ? this.routeText(dedupeKey, input, now)
        : this.routeCommand(dedupeKey, command, input.receivedAt, now);
    await this.recordDeliveryIntent(dedupeKey, now);
    await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
    return routed;
  }

  /**
   * One cron tick: at most one prompt, and silence whenever a prompt is already
   * in flight, answerable, awaiting a rating, or a test is running.
   */
  async enqueueDailyReview(input: {
    readonly dedupeKey: string;
    readonly nowUtc: string;
  }): Promise<Routed | "silent"> {
    if (this.findByDedupeKey(input.dedupeKey) !== null) {
      await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
      return "duplicate";
    }
    const now = new Date(input.nowUtc);
    const mode = this.store.activeMode();
    if (
      this.store.answerablePrompt() !== null ||
      this.store.awaitingRating() !== null ||
      mode === StudyMode.TEST_FORWARD ||
      mode === StudyMode.TEST_REVERSE ||
      this.store.inFlightDelivery()
    ) {
      return "silent";
    }
    if (this.store.localHour(now) < this.reviewHour && !this.store.overdueBacklog(now)) {
      return "silent";
    }
    const started = this.store.startReview(now);
    if (
      started.status !== StudyStartStatus.STARTED &&
      started.status !== StudyStartStatus.RESUMED
    ) {
      return "silent";
    }
    const prompt = this.prepareStudyPrompt(now);
    if (prompt === null) return "silent";
    const routed = this.insertReadyEvent(
      input.dedupeKey,
      "daily_review",
      prompt.promptText,
      input.nowUtc,
      prompt.id,
    );
    await this.recordDeliveryIntent(input.dedupeKey, now);
    await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
    return routed;
  }

  async importSnapshot(snapshot: SnapshotV2): Promise<SnapshotSummary> {
    const parsed = parseSnapshot(snapshot);
    if (parsed === null) throw new TypeError("Invalid SnapshotV2");
    const inboxCount = this.ctx.storage.sql.exec<{ count: number }>(
      "SELECT COUNT(*) AS count FROM inbox_events",
    ).one().count;
    if (inboxCount !== 0) throw new Error("snapshot import requires empty inbox");
    writeSnapshot(this.ctx.storage, parsed);
    return summarizeSnapshot(parsed, await sha256Snapshot(parsed));
  }

  async exportSnapshot(): Promise<SnapshotV2 | null> {
    const unfinished = this.ctx.storage.sql.exec<{ count: number }>(
      "SELECT COUNT(*) AS count FROM inbox_events WHERE status <> 'completed'",
    ).one().count;
    return unfinished === 0 ? readSnapshot(this.ctx.storage) : null;
  }

  async summary(): Promise<SnapshotSummary & { pendingInbox: number; failedInbox: number }> {
    const snapshot = readSnapshot(this.ctx.storage);
    const counts = this.ctx.storage.sql.exec<{ pending: number; failed: number }>(
      `SELECT
         COALESCE(SUM(CASE WHEN status IN ('pending', 'waiting', 'ready') THEN 1 ELSE 0 END), 0) AS pending,
         COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed
       FROM inbox_events`,
    ).one();
    return {
      ...summarizeSnapshot(snapshot, await sha256Snapshot(snapshot)),
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

  // ------------------------------------------------------------ inbound routing

  private routeCommand(
    dedupeKey: string,
    command: StudyCommand,
    createdAt: string,
    now: Date,
  ): Routed {
    if (command.kind === "usage") {
      const usage =
        command.command === "review"
          ? REVIEW_USAGE
          : command.command === "endstudy"
            ? ENDSTUDY_USAGE
            : TEST_USAGE;
      return this.insertReadyEvent(dedupeKey, "telegram", usage, createdAt, null);
    }
    if (command.kind === "endstudy") {
      const text =
        this.store.activeMode() === null
          ? "There is no active vocabulary study session."
          : this.store.exitStudy(now) === StudyMutationStatus.COMPLETED
            ? "Review exited. Unfinished cards are still due."
            : "I couldn't exit that session. Please try again.";
      return this.insertReadyEvent(dedupeKey, "telegram", text, createdAt, null);
    }
    const started =
      command.kind === "review"
        ? this.store.startReview(now)
        : this.store.startTest(command.direction, now);
    if (
      started.status === StudyStartStatus.STARTED ||
      started.status === StudyStartStatus.RESUMED
    ) {
      const prompt = this.prepareStudyPrompt(now);
      if (prompt === null) {
        const failure =
          command.kind === "review"
            ? "I couldn't prepare the review. Please try again."
            : "I couldn't prepare the test. Please try again.";
        return this.insertReadyEvent(dedupeKey, "telegram", failure, createdAt, null);
      }
      return this.insertReadyEvent(dedupeKey, "telegram", prompt.promptText, createdAt, prompt.id);
    }
    if (started.status === StudyStartStatus.EMPTY) {
      if (command.kind === "review") {
        return this.insertReadyEvent(
          dedupeKey,
          "telegram",
          "There are no eligible vocabulary cards to review.",
          createdAt,
          null,
        );
      }
      const available = started.availableCount ?? 0;
      return this.insertReadyEvent(
        dedupeKey,
        "telegram",
        `You have ${available} eligible distinct ${command.direction} entries. ` +
          `Add or unbury ${Math.max(TEST_REQUIRED_CARDS - available, 0)} more to start.`,
        createdAt,
        null,
      );
    }
    if (started.status === StudyStartStatus.CONFLICT) {
      if (command.kind === "review") {
        return this.insertReadyEvent(
          dedupeKey,
          "telegram",
          "Finish or exit your active test first.",
          createdAt,
          null,
        );
      }
      const active = this.store.activeMode();
      const activeText =
        active === StudyMode.REVIEW
          ? "review"
          : active === null
            ? "study session"
            : `${active.replace("test_", "")} test`;
      return this.insertReadyEvent(
        dedupeKey,
        "telegram",
        `Finish or exit your active ${activeText} first.`,
        createdAt,
        null,
      );
    }
    const failure =
      command.kind === "review"
        ? "I couldn't start the review. Please try again."
        : "I couldn't start the test. Please try again.";
    return this.insertReadyEvent(dedupeKey, "telegram", failure, createdAt, null);
  }

  /**
   * Plain-text precedence: settle a rating, then answer the delivered prompt,
   * then surface study work that is due but unanswerable, then capture.
   */
  private routeText(dedupeKey: string, input: AdmittedTelegramMessage, now: Date): Routed {
    const awaiting = this.store.awaitingRating();
    if (awaiting !== null) {
      return this.insertStudyAnswer(dedupeKey, awaiting.id, input);
    }
    const answerable = this.store.answerablePrompt();
    if (answerable !== null) {
      if (!isHintRequest(input.text)) {
        return this.insertStudyAnswer(dedupeKey, answerable.id, input);
      }
      const context = this.store.currentAnswerContext();
      return this.insertReadyEvent(
        dedupeKey,
        "telegram",
        context === null ? REVIEW_ERROR_REPLY : formatHint(context.entry),
        input.receivedAt,
        null,
      );
    }

    const snapshot = this.store.snapshot(now);
    const prompt = snapshot?.currentPrompt ?? null;
    if (snapshot !== null && prompt !== null && prompt.status === StudyPromptStatus.PREPARED) {
      const label = snapshot.mode === StudyMode.REVIEW ? "Review due." : "Test in progress.";
      return this.insertReadyEvent(
        dedupeKey,
        "telegram",
        interruption(label, prompt.promptText, input.text, "study session"),
        input.receivedAt,
        prompt.id,
      );
    }
    // A review session whose prepared prompt was cancelled by a day rollover
    // is active but has no live prompt; it needs surfacing just like the
    // no-session case.
    const rolloverGap =
      snapshot !== null &&
      prompt === null &&
      snapshot.mode === StudyMode.REVIEW &&
      snapshot.status === StudySessionStatus.ACTIVE;
    if (
      rolloverGap ||
      (snapshot === null && this.store.dueButNotAnswerable(now) && !this.store.studyWasExited())
    ) {
      // Due work exists but no prompt is outstanding (for example the machine
      // was off, or the day rolled over). Ordinary text must surface that work
      // instead of being graded or captured.
      const started = rolloverGap
        ? { status: StudyStartStatus.RESUMED }
        : this.store.startReview(now);
      if (
        started.status === StudyStartStatus.STARTED ||
        started.status === StudyStartStatus.RESUMED
      ) {
        const prepared = this.prepareStudyPrompt(now);
        if (prepared !== null) {
          return this.insertReadyEvent(
            dedupeKey,
            "telegram",
            interruption("Review due.", prepared.promptText, input.text, "review"),
            input.receivedAt,
            prepared.id,
          );
        }
      }
      return this.insertReadyEvent(dedupeKey, "telegram", REVIEW_ERROR_REPLY, input.receivedAt, null);
    }

    return this.routeCapture(dedupeKey, input);
  }

  private insertStudyAnswer(
    dedupeKey: string,
    promptId: number,
    input: AdmittedTelegramMessage,
  ): Routed {
    const payload: LanePayload = { lane: "study", promptId, answerText: input.text };
    this.insertEvent(dedupeKey, "telegram", "pending", payload, null, null, null, input.receivedAt);
    return "enqueued";
  }

  private routeCapture(dedupeKey: string, input: AdmittedTelegramMessage): Routed {
    const normalized = normalizeEntryText(input.text);
    if (normalized.status === EntryTextStatus.EMPTY) {
      return this.insertReadyEvent(dedupeKey, "telegram", EMPTY_REPLY, input.receivedAt, null);
    }
    if (normalized.status === EntryTextStatus.TOO_LONG) {
      return this.insertReadyEvent(dedupeKey, "telegram", TOO_LONG_REPLY, input.receivedAt, null);
    }
    if (normalized.status !== EntryTextStatus.VALID) {
      throw new Error("unreachable entry normalization status");
    }
    const existing = this.store.getEntry(normalized.normalizedText);
    if (existing !== null) {
      return this.insertReadyEvent(
        dedupeKey,
        "telegram",
        formatEntry(existing, "Already saved."),
        input.receivedAt,
        null,
        normalized.normalizedText,
      );
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
        null,
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
    return "enqueued";
  }

  // ------------------------------------------------------------- preparation

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
    if (payload.lane === "capture") {
      await this.prepareCapture(event, payload.displayText);
      return;
    }
    const now = new Date(event.created_at);
    const prepared = await this.prepareStudyAnswer(payload.promptId, payload.answerText, now);
    this.readyEvent(event.id, prepared.text, prepared.promptId);
    await this.recordDeliveryIntent(event.dedupe_key, now);
  }

  private async prepareCapture(event: InboxRow, displayText: string): Promise<void> {
    const definition = await this.provider.defineEntry(displayText);
    let response: string;
    if (definition.status === DefinitionStatus.NOT_FOUND) {
      response = NOT_FOUND_REPLY;
    } else if (definition.status !== DefinitionStatus.FOUND) {
      response = DEFINITION_ERROR_REPLY;
    } else {
      const result = this.store.captureEntry(displayText, definition.cards);
      if (result.status === CaptureStatus.STORAGE_ERROR) response = STORAGE_ERROR_REPLY;
      else if (result.entry === null || result.status === CaptureStatus.INVALID) {
        response = DEFINITION_ERROR_REPLY;
      } else {
        response = formatEntry(
          result.entry,
          result.status === CaptureStatus.SAVED ? "✓ Saved." : "Already saved.",
        );
      }
    }
    this.ctx.storage.transactionSync(() => {
      this.readyEvent(event.id, response, null);
      const followers = rows(
        this.ctx.storage.sql.exec<{ id: number }>(
          "SELECT id FROM inbox_events WHERE status = 'waiting' AND coalesced_to_event_id = ? ORDER BY id",
          event.id,
        ),
      );
      for (const follower of followers) this.readyEvent(follower.id, response, null);
    });
  }

  /**
   * One study step, mirroring the plugin's continue-study contract: evaluate a
   * fresh answer, or settle an evaluated one with an effort rating.
   */
  private async prepareStudyAnswer(
    promptId: number,
    answerText: string,
    now: Date,
  ): Promise<{ text: string; promptId: number | null }> {
    const context = this.store.currentAnswerContext();
    if (context === null) return { text: NO_ACTIVE_REPLY, promptId: null };
    if (context.prompt.id !== promptId) return { text: STALE_PROMPT_REPLY, promptId: null };

    if (context.draft !== null) {
      const choices = allowedRatings(context.draft.evaluation.grade);
      if (context.draft.evaluation.grade === EvaluationGrade.INCORRECT) {
        return this.settleStudyStep(context, "again", now);
      }
      const rating = parseRating(answerText, choices);
      if (rating === null) return { text: INVALID_RATING_REPLY, promptId: null };
      return this.settleStudyStep(context, rating, now);
    }
    if (trimPythonWhitespace(answerText).length === 0) {
      return { text: INVALID_ANSWER_REPLY, promptId: null };
    }

    const evaluation = await this.evaluateAnswer(context, answerText);
    if (evaluation === null) return { text: EVALUATION_ERROR_REPLY, promptId: null };
    if (this.store.recordAnswer(context.prompt.id, answerText, evaluation, now) === null) {
      return { text: STUDY_STORAGE_ERROR_REPLY, promptId: null };
    }
    const persisted = this.store.currentAnswerContext();
    if (persisted === null || persisted.draft === null) {
      return { text: STALE_PROMPT_REPLY, promptId: null };
    }
    const choices = allowedRatings(persisted.draft.evaluation.grade);
    if (persisted.draft.evaluation.grade === EvaluationGrade.INCORRECT) {
      return this.settleStudyStep(persisted, "again", now);
    }
    return { text: formatStudyEvaluation(persisted, choices), promptId: null };
  }

  private async evaluateAnswer(
    context: StudyAnswerContext,
    answerText: string,
  ): Promise<Evaluation | null> {
    if (answerText !== SHOW_ANSWER && caseFold(trimPythonWhitespace(answerText)) === "idk") {
      return { grade: EvaluationGrade.INCORRECT, feedback: IDK_FEEDBACK };
    }
    if (
      answerText !== SHOW_ANSWER &&
      context.queueItem.card.direction === CardDirection.REVERSE
    ) {
      // A reverse card asks for the saved entry itself, so it is graded by exact
      // match rather than by the evaluator.
      return normalizeReverseAnswer(answerText) === normalizeReverseAnswer(context.entry.displayText)
        ? { grade: EvaluationGrade.CORRECT, feedback: REVERSE_CORRECT_FEEDBACK }
        : { grade: EvaluationGrade.INCORRECT, feedback: REVERSE_INCORRECT_FEEDBACK };
    }
    const evaluated = await this.provider.evaluateAnswer(context.entry, answerText);
    return evaluated.status === EvaluationStatus.VALID ? evaluated.evaluation : null;
  }

  /** Finalize the rated step and hand back the next prompt, if any. */
  private settleStudyStep(
    context: StudyAnswerContext,
    rating: ReviewRating,
    now: Date,
  ): { text: string; promptId: number | null } {
    const finalized = this.store.finalize(context.prompt.id, rating, now);
    if (finalized.status !== FinalizeStatus.COMPLETED) {
      return {
        text:
          finalized.status === FinalizeStatus.STORAGE_ERROR
            ? STUDY_STORAGE_ERROR_REPLY
            : STALE_PROMPT_REPLY,
        promptId: null,
      };
    }
    const snapshot = finalized.snapshot!;
    const transition = finalized.transition!;
    const next =
      snapshot.status === StudySessionStatus.ACTIVE ? this.prepareStudyPrompt(now) : null;

    if (snapshot.status === StudySessionStatus.COMPLETED && snapshot.mode !== StudyMode.REVIEW) {
      const totals = this.store.summary(snapshot.sessionId);
      if (totals !== null) {
        const direction =
          snapshot.mode === StudyMode.TEST_FORWARD ? CardDirection.FORWARD : CardDirection.REVERSE;
        return {
          text: this.withEvaluation(context, formatDirectionalTotals(direction, totals)),
          promptId: null,
        };
      }
    }
    const schedule = formatStudySchedule(rating, transition.effectiveDue, snapshot.progress, {
      retryQueued: transition.retrySameSession,
      timeZone: this.timeZone,
      nextPrompt: next?.promptText ?? null,
    });
    return { text: this.withEvaluation(context, schedule), promptId: next?.id ?? null };
  }

  /** An incorrect answer reveals the canonical entry before the continuation. */
  private withEvaluation(context: StudyAnswerContext, continuation: string): string {
    return context.draft?.evaluation.grade === EvaluationGrade.INCORRECT
      ? `${formatStudyEvaluationResult(context)}\n\n${continuation}`
      : continuation;
  }

  private prepareStudyPrompt(now: Date): StudyPromptSnapshot | null {
    const plan = this.store.currentPromptPlan(now);
    if (plan === null) return null;
    if (plan.snapshot.currentPrompt !== null) return plan.snapshot.currentPrompt;
    const text = formatStudyPrompt(plan.context, plan.snapshot, {
      dueBacklog: Math.max(plan.snapshot.progress.total - plan.snapshot.progress.completed, 0),
    });
    return this.store.prepareCurrentPrompt(plan.promptKey, text, now);
  }

  // -------------------------------------------------------------- delivery

  /**
   * Claim the outbound send before it happens: until a receipt or a failure
   * lands, the ticker treats this prompt as in flight and stays quiet.
   */
  private async recordDeliveryIntent(dedupeKey: string, now: Date): Promise<void> {
    const event = this.findByDedupeKey(dedupeKey);
    if (event === null || event.status !== "ready") return;
    if (event.prepared_target_id === null || event.response_text === null) return;
    this.store.recordDeliveryIntent(event.prepared_target_id, {
      deliveryId: event.dedupe_key,
      contentFingerprint: await sha256Hex(event.response_text),
      now,
    });
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
      const sent = await this.telegram.sendText(chunks[event.next_chunk_index]!);
      const messageIds = [...this.deliveredMessageIds(event), ...sent];
      const next = event.next_chunk_index + 1;
      if (next >= chunks.length) {
        await this.confirmDelivery(event, messageIds);
        this.completeDelivery(event.id);
        return false;
      }
      rows(this.ctx.storage.sql.exec(
        `UPDATE inbox_events
         SET next_chunk_index = ?, attempt_count = 0, payload = ?, updated_at = ?, last_error = NULL
         WHERE id = ? AND status = 'ready'`,
        next,
        JSON.stringify(messageIds),
        timestamp(),
        event.id,
      ));
      await this.ctx.storage.setAlarm(Date.now() + ACTIONABLE_ALARM_DELAY_MS);
      return false;
    } catch {
      const attempts = event.attempt_count + 1;
      if (attempts < SEND_RETRY_DELAYS_SECONDS.length) {
        rows(this.ctx.storage.sql.exec(
          `UPDATE inbox_events
           SET attempt_count = ?, updated_at = ?, last_error = 'telegram_delivery_failed'
           WHERE id = ? AND status = 'ready'`,
          attempts,
          timestamp(),
          event.id,
        ));
        await this.ctx.storage.setAlarm(
          Date.now() + SEND_RETRY_DELAYS_SECONDS[attempts - 1]! * 1_000,
        );
        return true;
      }
      // Terminal failure: the prompt stays prepared, so it is never answerable
      // and the next tick may retry it.
      if (event.prepared_target_id !== null) {
        this.store.recordDeliveryFailure(event.prepared_target_id, {
          error: "telegram_delivery_failed",
          deliveryId: event.dedupe_key,
        });
      }
      rows(this.ctx.storage.sql.exec(
        `UPDATE inbox_events
         SET status = 'failed', attempt_count = ?, updated_at = ?,
             last_error = 'telegram_delivery_failed', payload = NULL
         WHERE id = ? AND status = 'ready'`,
        attempts,
        timestamp(),
        event.id,
      ));
      console.error(JSON.stringify({
        event: "inbox_delivery_failed",
        eventId: event.id,
        kind: event.kind,
        attemptCount: attempts,
      }));
      return false;
    }
  }

  private deliveredMessageIds(event: InboxRow): number[] {
    if (event.payload === null) return [];
    try {
      const parsed: unknown = JSON.parse(event.payload);
      return Array.isArray(parsed) ? parsed.filter((value) => Number.isSafeInteger(value)) : [];
    } catch {
      return [];
    }
  }

  /** Every chunk landed, so the prompt this response carried is now answerable. */
  private async confirmDelivery(event: InboxRow, messageIds: readonly number[]): Promise<void> {
    if (event.prepared_target_id === null || event.response_text === null) return;
    this.store.recordDelivery(event.prepared_target_id, {
      deliveryId: messageIds.length > 0 ? messageIds.join(",") : event.dedupe_key,
      contentFingerprint: await sha256Hex(event.response_text),
    });
  }

  // ----------------------------------------------------------- inbox storage

  private insertReadyEvent(
    dedupeKey: string,
    kind: InboxKind,
    response: string,
    createdAt: string,
    preparedTargetId: number | null,
    normalizedKey: string | null = null,
  ): Routed {
    if (this.findByDedupeKey(dedupeKey) !== null) return "duplicate";
    this.insertEvent(
      dedupeKey,
      kind,
      "ready",
      null,
      response,
      preparedTargetId,
      normalizedKey,
      createdAt,
    );
    return "enqueued";
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

  private readyEvent(id: number, response: string, preparedTargetId: number | null): void {
    rows(this.ctx.storage.sql.exec(
      `UPDATE inbox_events
       SET status = 'ready', response_text = ?, payload = NULL, prepared_target_id = ?,
           next_chunk_index = 0, attempt_count = 0, updated_at = ?, last_error = NULL
       WHERE id = ? AND status IN ('pending', 'waiting')`,
      response,
      preparedTargetId,
      timestamp(),
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
}
