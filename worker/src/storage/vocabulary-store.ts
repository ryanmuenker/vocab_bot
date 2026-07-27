import {
  CaptureStatus,
  CardDirection,
  CardScheduleState,
  EntryTextStatus,
  EvaluationGrade,
  FinalizeStatus,
  StudyMode,
  StudyMutationStatus,
  StudyPromptStatus,
  StudyQueueStatus,
  StudySessionStatus,
  StudyStartStatus,
} from "../domain/models";
import type {
  EntryCaptureResult,
  Evaluation,
  EvaluationGrade as EvaluationGradeValue,
  FinalizeResult,
  ReviewRating,
  SenseCard,
  StudyAnswerContext,
  StudyCardSnapshot,
  StudyDraftSnapshot,
  StudyPromptPlan,
  StudyPromptSnapshot,
  StudyQueueItemSnapshot,
  StudySnapshot,
  StudyStartResult,
  TestSummary,
  VocabularyEntry,
  VocabularySense,
} from "../domain/models";
import {
  normalizeEntryText,
  trimPythonWhitespace,
  validateSenseCards,
} from "../domain/normalization";
import {
  DESIRED_RETENTION,
  PARAMETERS_VERSION,
  PARAMETER_FINGERPRINT,
  SCHEDULER_KIND,
  SCHEDULER_VERSION,
  createCardSchedule,
  retrievability,
  transition,
} from "../domain/scheduling";
import type { CardSchedule, ScheduleTransition } from "../domain/scheduling";

/** Unseen cards introduced per local day, shared by review and test selection. */
const DAILY_NEW_CARD_LIMIT = 5;
/** A directional test always runs exactly five cards from five distinct entries. */
export const TEST_REQUIRED_CARDS = 5;
/** Position offsets that keep UNIQUE (session_id, position) satisfied mid-renumber. */
const ROLLOVER_OFFSET = 100_000;
const RETIRED_OFFSET = 200_000;

const VALID_GRADES: Record<EvaluationGradeValue, true> = {
  [EvaluationGrade.CORRECT]: true,
  [EvaluationGrade.PARTIAL]: true,
  [EvaluationGrade.INCORRECT]: true,
};

/** Thrown when a compare-and-set loses; unwinds the surrounding transaction. */
class StaleWrite extends Error {}

type SqlRow = Record<string, SqlStorageValue>;

function all<T extends SqlRow>(cursor: SqlStorageCursor<T>): T[] {
  return Array.from(cursor);
}

function oneOrNull<T extends SqlRow>(cursor: SqlStorageCursor<T>): T | null {
  return all(cursor)[0] ?? null;
}

function isoTimestamp(now: Date): string {
  return now.toISOString().replace(/\.000Z$/u, "Z");
}

function optionalTimestamp(value: string | null): Date | null {
  return value === null ? null : new Date(value);
}

interface ZonedFields {
  readonly year: number;
  readonly month: number;
  readonly day: number;
  readonly hour: number;
  readonly minute: number;
  readonly second: number;
}

const ZONED_FORMATTERS = new Map<string, Intl.DateTimeFormat>();

function zonedFields(at: Date, timeZone: string): ZonedFields {
  let formatter = ZONED_FORMATTERS.get(timeZone);
  if (formatter === undefined) {
    formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    ZONED_FORMATTERS.set(timeZone, formatter);
  }
  const parts: Record<string, string> = {};
  for (const part of formatter.formatToParts(at)) parts[part.type] = part.value;
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
    second: Number(parts.second),
  };
}

function localDate(at: Date, timeZone: string): string {
  const { year, month, day } = zonedFields(at, timeZone);
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function addLocalDays(date: string, days: number): string {
  const shifted = new Date(`${date}T00:00:00Z`);
  shifted.setUTCDate(shifted.getUTCDate() + days);
  return shifted.toISOString().slice(0, 10);
}

/**
 * The UTC instant of local midnight starting `date`. The offset is sampled at
 * the naive instant and then re-sampled at the candidate so a DST shift that
 * straddles midnight resolves to the offset actually in force.
 */
export function localMidnightUtc(date: string, timeZone: string): Date {
  const naive = Date.parse(`${date}T00:00:00Z`);
  const offsetAt = (instant: number): number => {
    const { year, month, day, hour, minute, second } = zonedFields(new Date(instant), timeZone);
    // Whole-second wall-clock reconstruction keeps fractional-offset zones
    // (UTC+05:30, UTC+05:45, UTC-03:30) exact, matching Python's ZoneInfo.
    return Date.UTC(year, month - 1, day, hour, minute, second) - Math.floor(instant / 1000) * 1000;
  };
  const first = offsetAt(naive);
  const candidate = naive - first;
  const second = offsetAt(candidate);
  return new Date(second === first ? candidate : naive - second);
}

interface EntryRow extends SqlRow {
  id: number;
  display_text: string;
  normalized_text: string;
  date_added: string;
  last_reviewed: string | null;
  review_status: "new" | "reviewed";
}

interface SenseRow extends SqlRow {
  id: number;
  entry_id: number;
  definition: string;
  part_of_speech: string;
  example_sentence: string;
  source_context: string | null;
  date_added: string;
}

interface CardRow extends SqlRow {
  id: number;
  entry_id: number;
  sense_id: number | null;
  direction: CardDirection;
  state: CardScheduleState;
  stability: number | null;
  difficulty: number | null;
  due_at: string;
  effective_due_at: string;
  last_review_at: string | null;
  repetitions: number;
  lapses: number;
  scheduler_kind: string;
  scheduler_version: string;
  parameters_version: string;
  parameter_fingerprint: string;
  desired_retention: number;
  introduced_local_date: string | null;
  buried_until_local_date: string | null;
  created_at: string;
}

interface StudySessionRow extends SqlRow {
  id: number;
  mode: StudyMode;
  status: StudySessionStatus;
  started_at: string;
  completed_at: string | null;
  local_date: string;
}

/** A queue row joined to its card, with the card columns kept unprefixed. */
type QueueCardRow = CardRow & {
  queue_id: number;
  queue_position: number;
  queue_status: StudyQueueStatus;
  retry_of_queue_item_id: number | null;
};

interface PromptRow extends SqlRow {
  id: number;
  session_id: number;
  queue_item_id: number;
  prompt_key: string;
  prompt_text: string;
  status: StudyPromptStatus;
  prepared_at: string;
  delivered_at: string | null;
  answered_at: string | null;
}

interface DraftRow extends SqlRow {
  id: number;
  prompt_id: number;
  submitted_answer: string;
  evaluator_grade: EvaluationGradeValue;
  evaluation_feedback: string;
  answered_at: string;
  created_at: string;
}

const QUEUE_CARD_COLUMNS = `q.id AS queue_id, q.position AS queue_position,
       q.status AS queue_status, q.retry_of_queue_item_id, c.*`;

function senseFromRow(row: SenseRow): VocabularySense {
  return {
    id: row.id,
    entryId: row.entry_id,
    definition: row.definition,
    partOfSpeech: row.part_of_speech,
    exampleSentence: row.example_sentence,
    sourceContext: row.source_context,
    dateAdded: row.date_added,
  };
}

function cardSnapshot(row: CardRow): StudyCardSnapshot {
  return {
    id: row.id,
    entryId: row.entry_id,
    senseId: row.sense_id,
    direction: row.direction,
    state: row.state,
    stability: row.stability,
    difficulty: row.difficulty,
    due: new Date(row.due_at),
    effectiveDue: new Date(row.effective_due_at),
    lastReview: optionalTimestamp(row.last_review_at),
    repetitions: row.repetitions,
    lapses: row.lapses,
    createdAt: new Date(row.created_at),
  };
}

function queueItemSnapshot(row: QueueCardRow): StudyQueueItemSnapshot {
  return {
    id: row.queue_id,
    card: cardSnapshot(row),
    position: row.queue_position,
    status: row.queue_status,
    retryOfQueueItemId: row.retry_of_queue_item_id,
  };
}

function promptSnapshot(row: PromptRow): StudyPromptSnapshot {
  return {
    id: row.id,
    sessionId: row.session_id,
    queueItemId: row.queue_item_id,
    promptKey: row.prompt_key,
    promptText: row.prompt_text,
    status: row.status,
    preparedAt: new Date(row.prepared_at),
    deliveredAt: optionalTimestamp(row.delivered_at),
    answeredAt: optionalTimestamp(row.answered_at),
  };
}

function scheduleFromRow(row: CardRow): CardSchedule {
  return createCardSchedule({
    state: row.state,
    stability: row.stability,
    difficulty: row.difficulty,
    due: new Date(row.due_at),
    lastReview: optionalTimestamp(row.last_review_at),
    repetitions: row.repetitions,
    lapses: row.lapses,
    schedulerKind: row.scheduler_kind,
    schedulerVersion: row.scheduler_version,
    parametersVersion: row.parameters_version,
    parameterFingerprint: row.parameter_fingerprint,
    desiredRetention: row.desired_retention,
  });
}

function isEvaluation(value: Evaluation | null): value is Evaluation {
  return (
    value !== null &&
    Object.hasOwn(VALID_GRADES, value.grade) &&
    typeof value.feedback === "string" &&
    trimPythonWhitespace(value.feedback).length > 0
  );
}

/** Ascending lexicographic order over equal-length numeric sort keys. */
function byKey<T>(left: readonly [readonly number[], T], right: readonly [readonly number[], T]): number {
  for (let index = 0; index < left[0].length; index += 1) {
    const difference = left[0][index]! - right[0][index]!;
    if (difference !== 0) return difference;
  }
  return 0;
}

interface SelectOptions {
  readonly maximumCount?: number;
  readonly includeSeenNonDue?: boolean;
  readonly direction?: CardDirection;
  readonly distinctEntries?: boolean;
  readonly excludedIds?: ReadonlySet<number>;
}

interface DeliveryRecord {
  readonly deliveryId: string;
  readonly contentFingerprint: string;
  readonly now?: Date;
}

interface DeliveryFailure {
  readonly error: string;
  readonly deliveryId?: string | null;
  readonly now?: Date;
}

/**
 * The persisted v5 study queue: capture, card projection, FSRS-6 scheduling,
 * and the prepared/delivered/answered prompt lifecycle that keeps a prompt
 * unanswerable until its text has provably reached the learner.
 */
export class VocabularyStore {
  constructor(
    private readonly storage: DurableObjectStorage,
    private readonly timeZone = "Asia/Kuala_Lumpur",
  ) {}

  private get sql(): SqlStorage {
    return this.storage.sql;
  }

  // ---------------------------------------------------------------- capture

  getEntry(text: string): VocabularyEntry | null {
    const normalized = normalizeEntryText(text);
    return normalized.status === EntryTextStatus.VALID
      ? this.loadEntryByNormalized(normalized.normalizedText)
      : null;
  }

  captureEntry(
    displayText: string,
    cards: readonly SenseCard[],
    now = new Date(),
  ): EntryCaptureResult {
    const normalized = normalizeEntryText(displayText);
    const validation = validateSenseCards(cards);
    if (normalized.status !== EntryTextStatus.VALID || !validation.valid) {
      return { status: CaptureStatus.INVALID, entry: null };
    }
    try {
      return this.storage.transactionSync(() => {
        const existing = this.loadEntryByNormalized(normalized.normalizedText);
        if (existing !== null) {
          return { status: CaptureStatus.ALREADY_EXISTS, entry: existing };
        }
        const timestamp = isoTimestamp(now);
        all(
          this.sql.exec(
            `INSERT INTO vocabulary_entries
              (display_text, normalized_text, date_added, review_status)
             VALUES (?, ?, ?, 'new')`,
            normalized.displayText,
            normalized.normalizedText,
            timestamp,
          ),
        );
        const entryId = this.lastInsertId();
        for (const card of validation.cards) {
          all(
            this.sql.exec(
              `INSERT INTO vocabulary_senses
                (entry_id, definition, part_of_speech, example_sentence, source_context, date_added)
               VALUES (?, ?, ?, ?, NULL, ?)`,
              entryId,
              card.definition,
              card.partOfSpeech,
              card.exampleSentence,
              timestamp,
            ),
          );
        }
        // Cards must exist the moment the entry does: projecting them later
        // would leave a freshly captured word permanently unreviewable.
        this.insertCards(entryId, now);
        const entry = this.loadEntryById(entryId);
        if (entry === null || entry.senses.length !== validation.cards.length) {
          throw new Error("incomplete capture aggregate");
        }
        return { status: CaptureStatus.SAVED, entry };
      });
    } catch {
      return { status: CaptureStatus.STORAGE_ERROR, entry: null };
    }
  }

  /** Project the missing forward and reverse cards of one entry. */
  createCards(entryId: number, now = new Date()): void {
    this.storage.transactionSync(() => this.insertCards(entryId, now));
  }

  private insertCards(entryId: number, now: Date): void {
    const timestamp = isoTimestamp(now);
    const insert = (senseId: number | null, direction: CardDirection): void => {
      all(
        this.sql.exec(
          `INSERT INTO vocabulary_cards (
             entry_id, sense_id, direction, state, due_at, effective_due_at,
             repetitions, lapses, scheduler_kind, scheduler_version,
             parameters_version, parameter_fingerprint, desired_retention, created_at
           ) VALUES (?, ?, ?, 'new', ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)`,
          entryId,
          senseId,
          direction,
          timestamp,
          timestamp,
          SCHEDULER_KIND,
          SCHEDULER_VERSION,
          PARAMETERS_VERSION,
          PARAMETER_FINGERPRINT,
          DESIRED_RETENTION,
          timestamp,
        ),
      );
    };
    const forward = oneOrNull(
      this.sql.exec<{ id: number }>(
        "SELECT id FROM vocabulary_cards WHERE entry_id = ? AND direction = 'forward'",
        entryId,
      ),
    );
    if (forward === null) insert(null, CardDirection.FORWARD);
    const senses = all(
      this.sql.exec<{ id: number }>(
        `SELECT s.id FROM vocabulary_senses s
         WHERE s.entry_id = ?
           AND NOT EXISTS (
             SELECT 1 FROM vocabulary_cards c
             WHERE c.sense_id = s.id AND c.direction = 'reverse'
           )
         ORDER BY s.id`,
        entryId,
      ),
    );
    for (const sense of senses) insert(sense.id, CardDirection.REVERSE);
  }

  // ------------------------------------------------------- session lifecycle

  startReview(now = new Date()): StudyStartResult {
    const today = localDate(now, this.timeZone);
    try {
      return this.storage.transactionSync<StudyStartResult>(() => {
        const open = this.openSession();
        if (open !== null) {
          if (open.mode !== StudyMode.REVIEW) {
            return { status: StudyStartStatus.CONFLICT, snapshot: null, availableCount: null };
          }
          this.reconcileRollover(open, now);
          return {
            status: StudyStartStatus.RESUMED,
            snapshot: this.buildSnapshot(open.id),
            availableCount: null,
          };
        }
        const carryover = this.carryoverCards(today);
        const excludedIds = new Set(carryover.cards.map((card) => card.id));
        const cards = [...carryover.cards, ...this.selectCards(now, { excludedIds })];
        if (cards.length === 0) {
          return { status: StudyStartStatus.EMPTY, snapshot: null, availableCount: 0 };
        }
        const sessionId = this.insertSession(StudyMode.REVIEW, now, today);
        this.enqueueCards(sessionId, cards, today, carryover.introductionDates);
        this.promoteFirstQueued(sessionId);
        return {
          status: StudyStartStatus.STARTED,
          snapshot: this.buildSnapshot(sessionId),
          availableCount: null,
        };
      });
    } catch {
      return { status: StudyStartStatus.STORAGE_ERROR, snapshot: null, availableCount: null };
    }
  }

  startTest(direction: CardDirection, now = new Date()): StudyStartResult {
    const mode =
      direction === CardDirection.FORWARD ? StudyMode.TEST_FORWARD : StudyMode.TEST_REVERSE;
    const today = localDate(now, this.timeZone);
    try {
      return this.storage.transactionSync<StudyStartResult>(() => {
        const open = this.openSession();
        if (open !== null) {
          return open.mode === mode
            ? {
                status: StudyStartStatus.RESUMED,
                snapshot: this.buildSnapshot(open.id),
                availableCount: null,
              }
            : { status: StudyStartStatus.CONFLICT, snapshot: null, availableCount: null };
        }
        const cards = this.selectCards(now, {
          maximumCount: TEST_REQUIRED_CARDS,
          includeSeenNonDue: true,
          direction,
          distinctEntries: true,
        });
        if (cards.length !== TEST_REQUIRED_CARDS) {
          return {
            status: StudyStartStatus.EMPTY,
            snapshot: null,
            availableCount: cards.length,
          };
        }
        const sessionId = this.insertSession(mode, now, today);
        this.enqueueCards(sessionId, cards, today);
        this.promoteFirstQueued(sessionId);
        return {
          status: StudyStartStatus.STARTED,
          snapshot: this.buildSnapshot(sessionId),
          availableCount: null,
        };
      });
    } catch {
      return { status: StudyStartStatus.STORAGE_ERROR, snapshot: null, availableCount: null };
    }
  }

  snapshot(now = new Date()): StudySnapshot | null {
    try {
      return this.storage.transactionSync<StudySnapshot | null>(() => {
        const session = this.openSession();
        if (session === null) return null;
        if (session.mode === StudyMode.REVIEW) this.reconcileRollover(session, now);
        return this.buildSnapshot(session.id);
      });
    } catch {
      return null;
    }
  }

  activeMode(): StudyMode | null {
    try {
      return this.openSession()?.mode ?? null;
    } catch {
      return null;
    }
  }

  exitStudy(now = new Date()): StudyMutationStatus {
    try {
      return this.storage.transactionSync<StudyMutationStatus>(() => {
        const session = this.openSession();
        if (session === null) return StudyMutationStatus.STALE;
        all(
          this.sql.exec(
            `UPDATE study_prompts SET status = 'cancelled'
             WHERE session_id = ? AND status IN ('prepared', 'delivered', 'answered')`,
            session.id,
          ),
        );
        all(
          this.sql.exec(
            `UPDATE study_queue SET status = 'skipped'
             WHERE session_id = ? AND status IN ('current', 'queued')`,
            session.id,
          ),
        );
        all(
          this.sql.exec(
            "UPDATE study_sessions SET status = 'exited', completed_at = ? WHERE id = ?",
            isoTimestamp(now),
            session.id,
          ),
        );
        return StudyMutationStatus.COMPLETED;
      });
    } catch {
      return StudyMutationStatus.STORAGE_ERROR;
    }
  }

  /** Correctness over the five original questions of a directional test. */
  summary(sessionId: number): TestSummary | null {
    try {
      const session = oneOrNull(
        this.sql.exec<{ mode: StudyMode }>(
          "SELECT mode FROM study_sessions WHERE id = ?",
          sessionId,
        ),
      );
      if (session === null || session.mode === StudyMode.REVIEW) return null;
      const rows = all(
        this.sql.exec<{ evaluator_grade: EvaluationGradeValue | null }>(
          `SELECT attempt.evaluator_grade
           FROM study_queue queue
           JOIN review_attempts attempt ON attempt.id = queue.completed_attempt_id
           WHERE queue.session_id = ? AND queue.retry_of_queue_item_id IS NULL`,
          sessionId,
        ),
      );
      const totals = { correct: 0, partial: 0, incorrect: 0 };
      for (const row of rows) {
        if (row.evaluator_grade !== null) totals[row.evaluator_grade] += 1;
      }
      return totals;
    } catch {
      return null;
    }
  }

  // -------------------------------------------------------- prompt lifecycle

  /** The card and session state needed to render the current prompt. */
  currentPromptPlan(now = new Date()): StudyPromptPlan | null {
    try {
      return this.storage.transactionSync<StudyPromptPlan | null>(() => {
        const session = this.openSession();
        if (session === null || session.status !== StudySessionStatus.ACTIVE) return null;
        if (session.mode === StudyMode.REVIEW) this.reconcileRollover(session, now);
        const snapshot = this.buildSnapshot(session.id);
        const row = oneOrNull(
          this.sql.exec<QueueCardRow>(
            `SELECT ${QUEUE_CARD_COLUMNS}
             FROM study_queue q JOIN vocabulary_cards c ON c.id = q.card_id
             WHERE q.session_id = ? AND q.status = 'current'`,
            session.id,
          ),
        );
        if (row === null) return null;
        const context = this.cardContext(row);
        if (context === null) return null;
        const promptKey =
          snapshot.mode === StudyMode.REVIEW
            ? `review:${session.id}:${row.queue_id}`
            : `test:${snapshot.mode}:${session.id}:${row.queue_id}`;
        return { snapshot, context, promptKey };
      });
    } catch {
      return null;
    }
  }

  /** Persist the current prompt as `prepared`; it is never delivered here. */
  prepareCurrentPrompt(
    promptKey: string,
    promptText: string,
    now = new Date(),
  ): StudyPromptSnapshot | null {
    if (promptKey.length === 0 || trimPythonWhitespace(promptText).length === 0) return null;
    try {
      return this.storage.transactionSync<StudyPromptSnapshot | null>(() => {
        const session = this.openSession();
        if (session === null) return null;
        if (session.mode === StudyMode.REVIEW) this.reconcileRollover(session, now);
        const queue = oneOrNull(
          this.sql.exec<{ id: number }>(
            "SELECT id FROM study_queue WHERE session_id = ? AND status = 'current'",
            session.id,
          ),
        );
        if (queue === null) return null;
        const existing = oneOrNull(
          this.sql.exec<PromptRow>(
            "SELECT * FROM study_prompts WHERE queue_item_id = ?",
            queue.id,
          ),
        );
        if (existing !== null) return promptSnapshot(existing);
        all(
          this.sql.exec(
            `INSERT INTO study_prompts
              (session_id, queue_item_id, prompt_key, prompt_text, status, prepared_at)
             VALUES (?, ?, ?, ?, 'prepared', ?)`,
            session.id,
            queue.id,
            promptKey,
            promptText,
            isoTimestamp(now),
          ),
        );
        return this.promptById(this.lastInsertId());
      });
    } catch {
      return null;
    }
  }

  /**
   * Claim an outbound send for a prepared prompt. The attempt has no receipt
   * yet, which is exactly what marks the prompt as in flight.
   */
  recordDeliveryIntent(promptId: number, record: DeliveryRecord): boolean {
    if (record.deliveryId.length === 0 || record.contentFingerprint.length === 0) return false;
    const timestamp = isoTimestamp(record.now ?? new Date());
    try {
      return this.storage.transactionSync<boolean>(() => {
        const prompt = this.promptById(promptId);
        if (prompt === null || prompt.status !== StudyPromptStatus.PREPARED) return false;
        const existing = oneOrNull(
          this.sql.exec<{ present: number }>(
            `SELECT 1 AS present FROM prompt_delivery_attempts
             WHERE prompt_id = ? AND outbound_delivery_id = ? AND content_fingerprint = ?`,
            promptId,
            record.deliveryId,
            record.contentFingerprint,
          ),
        );
        if (existing !== null) return true;
        this.insertDeliveryAttempt(promptId, {
          status: "unknown",
          attemptedAt: timestamp,
          receiptAt: null,
          deliveryId: record.deliveryId,
          contentFingerprint: record.contentFingerprint,
          errorText: null,
        });
        return true;
      });
    } catch {
      return false;
    }
  }

  /** Promote a prepared prompt to answerable after its text provably landed. */
  recordDelivery(promptId: number, record: DeliveryRecord): StudyPromptSnapshot | null {
    if (record.deliveryId.length === 0 || record.contentFingerprint.length === 0) return null;
    const timestamp = isoTimestamp(record.now ?? new Date());
    try {
      return this.storage.transactionSync<StudyPromptSnapshot | null>(() => {
        const prompt = this.promptById(promptId);
        if (prompt === null) return null;
        if (prompt.status === StudyPromptStatus.DELIVERED) return prompt;
        if (prompt.status !== StudyPromptStatus.PREPARED) return null;
        this.insertDeliveryAttempt(promptId, {
          status: "delivered",
          attemptedAt: timestamp,
          receiptAt: timestamp,
          deliveryId: record.deliveryId,
          contentFingerprint: record.contentFingerprint,
          errorText: null,
        });
        all(
          this.sql.exec(
            `UPDATE study_prompts SET status = 'delivered', delivered_at = ?
             WHERE id = ? AND status = 'prepared'`,
            timestamp,
            promptId,
          ),
        );
        return this.promptById(promptId);
      });
    } catch {
      return null;
    }
  }

  /** Record a terminal send failure; the prompt stays prepared and retryable. */
  recordDeliveryFailure(promptId: number, failure: DeliveryFailure): StudyPromptSnapshot | null {
    const timestamp = isoTimestamp(failure.now ?? new Date());
    try {
      return this.storage.transactionSync<StudyPromptSnapshot | null>(() => {
        const prompt = this.promptById(promptId);
        if (prompt === null || prompt.status !== StudyPromptStatus.PREPARED) return null;
        this.insertDeliveryAttempt(promptId, {
          status: "failed",
          attemptedAt: timestamp,
          receiptAt: timestamp,
          deliveryId: failure.deliveryId ?? null,
          contentFingerprint: null,
          errorText: failure.error,
        });
        return prompt;
      });
    } catch {
      return null;
    }
  }

  answerablePrompt(): StudyPromptSnapshot | null {
    return this.queryPrompt(StudyPromptStatus.DELIVERED);
  }

  awaitingRating(): StudyPromptSnapshot | null {
    return this.queryPrompt(StudyPromptStatus.ANSWERED);
  }

  /** Work is due but no prompt is answerable, so ordinary text is not an answer. */
  dueButNotAnswerable(now = new Date()): boolean {
    if (this.answerablePrompt() !== null) return false;
    try {
      return (
        oneOrNull(
          this.sql.exec<{ present: number }>(
            `SELECT 1 AS present FROM vocabulary_cards
             WHERE state != 'new' AND effective_due_at <= ?
               AND (buried_until_local_date IS NULL OR buried_until_local_date < ?)
             LIMIT 1`,
            isoTimestamp(now),
            localDate(now, this.timeZone),
          ),
        ) !== null
      );
    } catch {
      return false;
    }
  }

  /** A prepared prompt whose latest delivery attempt has no receipt yet. */
  inFlightDelivery(): boolean {
    try {
      return (
        oneOrNull(
          this.sql.exec<{ present: number }>(
            `SELECT 1 AS present FROM study_prompts p
             JOIN study_sessions s ON s.id = p.session_id
             JOIN study_queue q ON q.id = p.queue_item_id
             JOIN prompt_delivery_attempts a ON a.id = (
               SELECT MAX(id) FROM prompt_delivery_attempts WHERE prompt_id = p.id
             )
             WHERE s.status = 'active' AND q.status = 'current'
               AND p.status = 'prepared' AND a.receipt_at IS NULL
             LIMIT 1`,
          ),
        ) !== null
      );
    } catch {
      return false;
    }
  }

  /** Unburied seen cards whose effective due day is already in the past. */
  overdueBacklog(now = new Date()): boolean {
    const today = localDate(now, this.timeZone);
    try {
      return (
        oneOrNull(
          this.sql.exec<{ present: number }>(
            `SELECT 1 AS present FROM vocabulary_cards
             WHERE state != 'new' AND substr(effective_due_at, 1, 10) < ?
               AND (buried_until_local_date IS NULL OR buried_until_local_date < ?)
             LIMIT 1`,
            today,
            today,
          ),
        ) !== null
      );
    } catch {
      return false;
    }
  }

  localHour(now = new Date()): number {
    return zonedFields(now, this.timeZone).hour;
  }

  // ------------------------------------------------------------ answer flow

  currentAnswerContext(): StudyAnswerContext | null {
    try {
      const prompt = oneOrNull(
        this.sql.exec<PromptRow>(
          `SELECT p.* FROM study_prompts p
           JOIN study_sessions s ON s.id = p.session_id
           WHERE s.status IN ('active', 'interrupted')
             AND p.status IN ('delivered', 'answered')
           ORDER BY p.id DESC LIMIT 1`,
        ),
      );
      if (prompt === null) return null;
      const queue = oneOrNull(
        this.sql.exec<QueueCardRow>(
          `SELECT ${QUEUE_CARD_COLUMNS}
           FROM study_queue q JOIN vocabulary_cards c ON c.id = q.card_id
           WHERE q.id = ?`,
          prompt.queue_item_id,
        ),
      );
      if (queue === null) return null;
      const context = this.cardContext(queue);
      if (context === null) return null;
      const draftRow = oneOrNull(
        this.sql.exec<DraftRow>("SELECT * FROM answer_drafts WHERE prompt_id = ?", prompt.id),
      );
      const draft: StudyDraftSnapshot | null =
        draftRow === null
          ? null
          : {
              id: draftRow.id,
              submittedAnswer: draftRow.submitted_answer,
              evaluation: {
                grade: draftRow.evaluator_grade,
                feedback: draftRow.evaluation_feedback,
              },
              answeredAt: new Date(draftRow.answered_at),
            };
      return { ...context, prompt: promptSnapshot(prompt), draft };
    } catch {
      return null;
    }
  }

  /** Persist the learner's answer and its grade as an immutable draft. */
  recordAnswer(
    promptId: number,
    answerText: string,
    evaluation: Evaluation | null,
    now = new Date(),
  ): StudyPromptSnapshot | null {
    if (trimPythonWhitespace(answerText).length === 0 || !isEvaluation(evaluation)) return null;
    const timestamp = isoTimestamp(now);
    try {
      return this.storage.transactionSync<StudyPromptSnapshot | null>(() => {
        const prompt = this.promptById(promptId);
        if (prompt === null) return null;
        if (prompt.status === StudyPromptStatus.ANSWERED) return prompt;
        if (prompt.status !== StudyPromptStatus.DELIVERED) return null;
        all(
          this.sql.exec(
            `INSERT INTO answer_drafts
              (prompt_id, submitted_answer, evaluator_grade, evaluation_feedback, answered_at, created_at)
             VALUES (?, ?, ?, ?, ?, ?)`,
            promptId,
            answerText,
            evaluation.grade,
            evaluation.feedback,
            timestamp,
            timestamp,
          ),
        );
        all(
          this.sql.exec(
            `UPDATE study_prompts SET status = 'answered', answered_at = ?
             WHERE id = ? AND status = 'delivered'`,
            timestamp,
            promptId,
          ),
        );
        return this.promptById(promptId);
      });
    } catch {
      return null;
    }
  }

  /**
   * Apply one FSRS transition: write the attempt, advance the card, bury the
   * entry's other cards for the local day, and move the queue forward.
   */
  finalize(promptId: number, rating: ReviewRating, now = new Date()): FinalizeResult {
    try {
      return this.storage.transactionSync<FinalizeResult>(() => {
        const row = oneOrNull(
          this.sql.exec<FinalizeRow>(
            `SELECT p.id AS prompt_id, p.status AS prompt_status, p.session_id AS session_id,
                    q.id AS queue_id, q.card_id AS card_id,
                    q.retry_of_queue_item_id AS retry_of_queue_item_id,
                    original.completed_attempt_id AS retry_of_attempt_id,
                    d.id AS draft_id, d.submitted_answer AS submitted_answer,
                    d.evaluator_grade AS evaluator_grade,
                    d.evaluation_feedback AS evaluation_feedback,
                    c.id, c.entry_id, c.sense_id, c.direction, c.state, c.stability,
                    c.difficulty, c.due_at, c.effective_due_at, c.last_review_at,
                    c.repetitions, c.lapses, c.scheduler_kind, c.scheduler_version,
                    c.parameters_version, c.parameter_fingerprint, c.desired_retention,
                    c.introduced_local_date, c.buried_until_local_date, c.created_at
             FROM study_prompts p
             JOIN study_queue q ON q.id = p.queue_item_id
             LEFT JOIN study_queue original ON original.id = q.retry_of_queue_item_id
             JOIN vocabulary_cards c ON c.id = q.card_id
             LEFT JOIN answer_drafts d ON d.prompt_id = p.id
             WHERE p.id = ?`,
            promptId,
          ),
        );
        if (row === null || row.prompt_status === StudyPromptStatus.COMPLETED) {
          return { status: FinalizeStatus.STALE, transition: null, snapshot: null };
        }
        if (row.prompt_status !== StudyPromptStatus.ANSWERED || row.draft_id === null) {
          return { status: FinalizeStatus.NO_ANSWER, transition: null, snapshot: null };
        }

        const before = scheduleFromRow(row);
        const retryAgain = rating === "again" && row.retry_of_queue_item_id !== null;
        const result = transition(before, rating, now, {
          sameSessionRetry: retryAgain,
          dueFloorUtc: retryAgain ? this.nextLocalMidnight(now) : null,
        });
        const attemptId = this.insertAttempt(row, result, rating, retryAgain);
        const updated = this.sql.exec(
          `UPDATE vocabulary_cards
           SET state = ?, stability = ?, difficulty = ?, due_at = ?, effective_due_at = ?,
               last_review_at = ?, repetitions = ?, lapses = ?, scheduler_kind = ?,
               scheduler_version = ?, parameters_version = ?, parameter_fingerprint = ?,
               desired_retention = ?
           WHERE id = ? AND repetitions = ?`,
          result.after.state,
          result.after.stability,
          result.after.difficulty,
          isoTimestamp(result.rawDue),
          isoTimestamp(result.effectiveDue),
          isoTimestamp(now),
          result.after.repetitions,
          result.after.lapses,
          result.after.schedulerKind,
          result.after.schedulerVersion,
          result.after.parametersVersion,
          result.after.parameterFingerprint,
          result.after.desiredRetention,
          row.card_id,
          before.repetitions,
        );
        all(updated);
        // `rowsWritten` also counts index rows, so the guard reads the card
        // back instead: the repetition counter must have advanced exactly once.
        const persisted = oneOrNull(
          this.sql.exec<{ repetitions: number }>(
            "SELECT repetitions FROM vocabulary_cards WHERE id = ?",
            row.card_id,
          ),
        );
        if (persisted === null || persisted.repetitions !== result.after.repetitions) {
          throw new StaleWrite();
        }

        const today = localDate(now, this.timeZone);
        // One answer settles the whole entry for today: its siblings are buried
        // without touching their due times.
        all(
          this.sql.exec(
            "UPDATE vocabulary_cards SET buried_until_local_date = ? WHERE entry_id = ? AND id != ?",
            today,
            row.entry_id,
            row.card_id,
          ),
        );
        all(
          this.sql.exec(
            `UPDATE study_queue SET status = 'skipped'
             WHERE session_id = ? AND status = 'queued' AND card_id IN (
               SELECT id FROM vocabulary_cards WHERE entry_id = ? AND id != ?
             )`,
            row.session_id,
            row.entry_id,
            row.card_id,
          ),
        );
        all(
          this.sql.exec(
            "UPDATE study_prompts SET status = 'completed' WHERE id = ? AND status = 'answered'",
            promptId,
          ),
        );
        all(
          this.sql.exec(
            `UPDATE study_queue SET status = 'completed', completed_attempt_id = ?
             WHERE id = ? AND status = 'current'`,
            attemptId,
            row.queue_id,
          ),
        );
        if (result.retrySameSession) {
          const nextPosition =
            oneOrNull(
              this.sql.exec<{ position: number }>(
                "SELECT COALESCE(MAX(position), 0) + 1 AS position FROM study_queue WHERE session_id = ?",
                row.session_id,
              ),
            )?.position ?? 1;
          all(
            this.sql.exec(
              `INSERT INTO study_queue
                (session_id, card_id, position, status, retry_of_queue_item_id)
               VALUES (?, ?, ?, 'queued', ?)`,
              row.session_id,
              row.card_id,
              nextPosition,
              row.queue_id,
            ),
          );
        }
        if (this.promoteFirstQueued(row.session_id) === null) {
          all(
            this.sql.exec(
              `UPDATE study_sessions SET status = 'completed', completed_at = ?
               WHERE id = ? AND status IN ('active', 'interrupted')`,
              isoTimestamp(now),
              row.session_id,
            ),
          );
        }
        return {
          status: FinalizeStatus.COMPLETED,
          transition: result,
          snapshot: this.buildSnapshot(row.session_id),
        };
      });
    } catch (error) {
      return {
        status: error instanceof StaleWrite ? FinalizeStatus.STALE : FinalizeStatus.STORAGE_ERROR,
        transition: null,
        snapshot: null,
      };
    }
  }

  // ---------------------------------------------------------------- internals

  private lastInsertId(): number {
    const row = oneOrNull(this.sql.exec<{ id: number }>("SELECT last_insert_rowid() AS id"));
    if (row === null) throw new Error("missing inserted row id");
    return row.id;
  }

  private loadEntryById(id: number): VocabularyEntry | null {
    const row = oneOrNull(
      this.sql.exec<EntryRow>("SELECT * FROM vocabulary_entries WHERE id = ?", id),
    );
    return row === null ? null : this.entryFromRow(row);
  }

  private loadEntryByNormalized(normalizedText: string): VocabularyEntry | null {
    const row = oneOrNull(
      this.sql.exec<EntryRow>(
        "SELECT * FROM vocabulary_entries WHERE normalized_text = ?",
        normalizedText,
      ),
    );
    return row === null ? null : this.entryFromRow(row);
  }

  private entryFromRow(row: EntryRow): VocabularyEntry {
    const senses = all(
      this.sql.exec<SenseRow>(
        "SELECT * FROM vocabulary_senses WHERE entry_id = ? ORDER BY id",
        row.id,
      ),
    ).map(senseFromRow);
    return {
      id: row.id,
      displayText: row.display_text,
      normalizedText: row.normalized_text,
      dateAdded: row.date_added,
      lastReviewed: row.last_reviewed,
      reviewStatus: row.review_status,
      senses,
    };
  }

  private cardContext(row: QueueCardRow): {
    queueItem: StudyQueueItemSnapshot;
    entry: VocabularyEntry;
    sense: VocabularySense | null;
  } | null {
    const entry = this.loadEntryById(row.entry_id);
    if (entry === null) return null;
    let sense: VocabularySense | null = null;
    if (row.sense_id !== null) {
      sense = entry.senses.find((candidate) => candidate.id === row.sense_id) ?? null;
      if (sense === null) return null;
    }
    return { queueItem: queueItemSnapshot(row), entry, sense };
  }

  private openSession(): StudySessionRow | null {
    return oneOrNull(
      this.sql.exec<StudySessionRow>(
        `SELECT * FROM study_sessions
         WHERE status IN ('active', 'interrupted') ORDER BY id LIMIT 1`,
      ),
    );
  }

  private promptById(id: number): StudyPromptSnapshot | null {
    const row = oneOrNull(
      this.sql.exec<PromptRow>("SELECT * FROM study_prompts WHERE id = ?", id),
    );
    return row === null ? null : promptSnapshot(row);
  }

  private queryPrompt(status: StudyPromptStatus): StudyPromptSnapshot | null {
    try {
      const row = oneOrNull(
        this.sql.exec<PromptRow>(
          `SELECT p.* FROM study_prompts p
           JOIN study_sessions s ON s.id = p.session_id
           WHERE s.status IN ('active', 'interrupted') AND p.status = ?
           ORDER BY p.id DESC LIMIT 1`,
          status,
        ),
      );
      return row === null ? null : promptSnapshot(row);
    } catch {
      return null;
    }
  }

  private insertSession(mode: StudyMode, now: Date, today: string): number {
    all(
      this.sql.exec(
        `INSERT INTO study_sessions (mode, status, started_at, local_date)
         VALUES (?, 'active', ?, ?)`,
        mode,
        isoTimestamp(now),
        today,
      ),
    );
    return this.lastInsertId();
  }

  private insertDeliveryAttempt(
    promptId: number,
    attempt: {
      readonly status: "unknown" | "failed" | "delivered";
      readonly attemptedAt: string;
      readonly receiptAt: string | null;
      readonly deliveryId: string | null;
      readonly contentFingerprint: string | null;
      readonly errorText: string | null;
    },
  ): void {
    const number =
      oneOrNull(
        this.sql.exec<{ next: number }>(
          `SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next
           FROM prompt_delivery_attempts WHERE prompt_id = ?`,
          promptId,
        ),
      )?.next ?? 1;
    all(
      this.sql.exec(
        `INSERT INTO prompt_delivery_attempts
          (prompt_id, attempt_number, status, attempted_at, receipt_at,
           outbound_delivery_id, content_fingerprint, error_text)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        promptId,
        number,
        attempt.status,
        attempt.attemptedAt,
        attempt.receiptAt,
        attempt.deliveryId,
        attempt.contentFingerprint,
        attempt.errorText,
      ),
    );
  }

  /** Make the lowest queued item current; null when the queue is exhausted. */
  private promoteFirstQueued(sessionId: number): number | null {
    const next = oneOrNull(
      this.sql.exec<{ id: number }>(
        `SELECT id FROM study_queue WHERE session_id = ? AND status = 'queued'
         ORDER BY position LIMIT 1`,
        sessionId,
      ),
    );
    if (next === null) return null;
    all(this.sql.exec("UPDATE study_queue SET status = 'current' WHERE id = ?", next.id));
    return next.id;
  }

  private nextLocalMidnight(now: Date): Date {
    return localMidnightUtc(addLocalDays(localDate(now, this.timeZone), 1), this.timeZone);
  }

  /**
   * Due cards first, ordered by effective due then predicted recall then age;
   * then the weakest seen non-due cards for tests; then unseen cards within the
   * shared five-per-local-day introduction quota.
   */
  private selectCards(now: Date, options: SelectOptions = {}): StudyCardSnapshot[] {
    const today = localDate(now, this.timeZone);
    const rows =
      options.direction === undefined
        ? all(
            this.sql.exec<CardRow>(
              `SELECT * FROM vocabulary_cards
               WHERE (buried_until_local_date IS NULL OR buried_until_local_date < ?)`,
              today,
            ),
          )
        : all(
            this.sql.exec<CardRow>(
              `SELECT * FROM vocabulary_cards
               WHERE (buried_until_local_date IS NULL OR buried_until_local_date < ?)
                 AND direction = ?`,
              today,
              options.direction,
            ),
          );
    const introducedToday =
      oneOrNull(
        this.sql.exec<{ count: number }>(
          "SELECT COUNT(*) AS count FROM vocabulary_cards WHERE introduced_local_date = ?",
          today,
        ),
      )?.count ?? 0;
    const remainingNew = Math.max(DAILY_NEW_CARD_LIMIT - introducedToday, 0);
    const excluded = options.excludedIds ?? new Set<number>();

    const due: [readonly number[], StudyCardSnapshot][] = [];
    const weak: [readonly number[], StudyCardSnapshot][] = [];
    const unseen: [readonly number[], StudyCardSnapshot][] = [];
    for (const row of rows) {
      if (excluded.has(row.id)) continue;
      const card = cardSnapshot(row);
      if (card.state === CardScheduleState.NEW) {
        if (row.introduced_local_date === null) {
          unseen.push([[card.createdAt.getTime(), card.id], card]);
        }
        continue;
      }
      const recall = retrievability(scheduleFromRow(row), now);
      if (card.effectiveDue.getTime() <= now.getTime()) {
        due.push([[card.effectiveDue.getTime(), recall, card.createdAt.getTime(), card.id], card]);
      } else if (options.includeSeenNonDue === true) {
        weak.push([[recall, card.createdAt.getTime(), card.id], card]);
      }
    }
    due.sort(byKey);
    weak.sort(byKey);
    unseen.sort(byKey);

    const ordered: [StudyCardSnapshot, boolean][] = due.map(([, card]) => [card, false]);
    if (options.includeSeenNonDue === true) {
      for (const [, card] of weak) ordered.push([card, false]);
    }
    for (const [, card] of unseen) ordered.push([card, true]);

    const selected: StudyCardSnapshot[] = [];
    const entries = new Set<number>();
    let selectedNew = 0;
    for (const [card, isNew] of ordered) {
      if (isNew && selectedNew >= remainingNew) continue;
      if (options.distinctEntries === true && entries.has(card.entryId)) continue;
      entries.add(card.entryId);
      selected.push(card);
      if (isNew) selectedNew += 1;
      if (options.maximumCount !== undefined && selected.length >= options.maximumCount) break;
    }
    return selected;
  }

  /**
   * Cards left unanswered by the last exited review, so exiting never drops
   * work: they return in the next session with their due times untouched.
   */
  private carryoverCards(today: string): {
    cards: StudyCardSnapshot[];
    introductionDates: Map<number, string>;
  } {
    const session = oneOrNull(
      this.sql.exec<{ id: number; status: StudySessionStatus }>(
        "SELECT id, status FROM study_sessions WHERE mode = 'review' ORDER BY id DESC LIMIT 1",
      ),
    );
    if (session === null || session.status !== StudySessionStatus.EXITED) {
      return { cards: [], introductionDates: new Map() };
    }
    const rows = all(
      this.sql.exec<CardRow & { position: number }>(
        `SELECT q.position, c.*
         FROM study_queue q JOIN vocabulary_cards c ON c.id = q.card_id
         WHERE q.session_id = ?
           AND q.status = 'skipped'
           AND q.retry_of_queue_item_id IS NULL
           AND (c.buried_until_local_date IS NULL OR c.buried_until_local_date < ?)
           AND NOT EXISTS (
             SELECT 1 FROM study_queue completed
             WHERE completed.session_id = q.session_id
               AND completed.card_id = q.card_id
               AND completed.status = 'completed'
           )
           AND q.id = (
             SELECT MAX(latest.id) FROM study_queue latest
             WHERE latest.session_id = q.session_id
               AND latest.card_id = q.card_id
               AND latest.status = 'skipped'
               AND latest.retry_of_queue_item_id IS NULL
           )
         ORDER BY q.position`,
        session.id,
        today,
      ),
    );
    const introductionDates = new Map<number, string>();
    for (const row of rows) {
      if (row.state === CardScheduleState.NEW && row.introduced_local_date !== null) {
        introductionDates.set(row.id, row.introduced_local_date);
      }
    }
    return { cards: rows.map(cardSnapshot), introductionDates };
  }

  private enqueueCards(
    sessionId: number,
    cards: readonly StudyCardSnapshot[],
    today: string,
    introductionDates: ReadonlyMap<number, string> = new Map(),
  ): void {
    cards.forEach((card, index) => {
      const introduced =
        card.state === CardScheduleState.NEW ? introductionDates.get(card.id) ?? today : null;
      all(
        this.sql.exec(
          `INSERT INTO study_queue (session_id, card_id, position, status, introduced_local_date)
           VALUES (?, ?, ?, 'queued', ?)`,
          sessionId,
          card.id,
          index + 1,
          introduced,
        ),
      );
      if (introduced !== null) {
        all(
          this.sql.exec(
            `UPDATE vocabulary_cards
             SET introduced_local_date = COALESCE(introduced_local_date, ?)
             WHERE id = ?`,
            introduced,
            card.id,
          ),
        );
      }
    });
  }

  /**
   * Rebuild an open review queue once per local-day rollover: an in-flight
   * delivered or answered prompt keeps its position, a merely prepared one is
   * retired and its card re-selected, and the rest is re-ordered against
   * today's due set.
   */
  private reconcileRollover(session: StudySessionRow, now: Date): void {
    const today = localDate(now, this.timeZone);
    if (session.local_date === today) return;

    let prompt = oneOrNull(
      this.sql.exec<{ queue_item_id: number; status: StudyPromptStatus }>(
        `SELECT queue_item_id, status FROM study_prompts
         WHERE session_id = ? AND status IN ('prepared', 'delivered', 'answered')
         ORDER BY id DESC LIMIT 1`,
        session.id,
      ),
    );
    let activeRows = all(
      this.sql.exec<QueueCardRow>(
        `SELECT ${QUEUE_CARD_COLUMNS}
         FROM study_queue q JOIN vocabulary_cards c ON c.id = q.card_id
         WHERE q.session_id = ? AND q.status IN ('current', 'queued')
         ORDER BY q.position`,
        session.id,
      ),
    );

    let replacement: StudyCardSnapshot | null = null;
    if (prompt !== null && prompt.status === StudyPromptStatus.PREPARED) {
      const retired = activeRows.find((row) => row.queue_id === prompt!.queue_item_id);
      replacement = retired === undefined ? null : cardSnapshot(retired);
      all(
        this.sql.exec(
          "UPDATE study_prompts SET status = 'cancelled' WHERE queue_item_id = ?",
          prompt.queue_item_id,
        ),
      );
      all(
        this.sql.exec(
          `UPDATE study_queue SET status = 'skipped', position = position + ${RETIRED_OFFSET}
           WHERE id = ?`,
          prompt.queue_item_id,
        ),
      );
      activeRows = activeRows.filter((row) => row.queue_id !== prompt!.queue_item_id);
      prompt = null;
    }
    const pinnedQueueId =
      prompt !== null &&
      (prompt.status === StudyPromptStatus.DELIVERED ||
        prompt.status === StudyPromptStatus.ANSWERED)
        ? prompt.queue_item_id
        : null;

    const activeCardIds = new Set(activeRows.map((row) => row.id));
    const selected = this.selectCards(now, { excludedIds: activeCardIds });
    if (replacement !== null && !selected.some((card) => card.id === replacement!.id)) {
      selected.push(replacement);
    }

    type Placement = { readonly queueId: number } | { readonly card: StudyCardSnapshot };
    const dueItems: [readonly number[], Placement][] = [];
    const newItems: [readonly number[], Placement][] = [];
    for (const row of activeRows) {
      if (row.queue_id === pinnedQueueId) continue;
      const placement: Placement = { queueId: row.queue_id };
      if (row.state === CardScheduleState.NEW) {
        newItems.push([[new Date(row.created_at).getTime(), row.id], placement]);
      } else {
        dueItems.push([
          [
            new Date(row.effective_due_at).getTime(),
            new Date(row.created_at).getTime(),
            row.id,
          ],
          placement,
        ]);
      }
    }
    for (const card of selected) {
      const placement: Placement = { card };
      if (card.state === CardScheduleState.NEW) {
        newItems.push([[card.createdAt.getTime(), card.id], placement]);
      } else {
        dueItems.push([
          [card.effectiveDue.getTime(), card.createdAt.getTime(), card.id],
          placement,
        ]);
      }
    }
    dueItems.sort(byKey);
    newItems.sort(byKey);

    const ordered: Placement[] = [];
    if (pinnedQueueId !== null) ordered.push({ queueId: pinnedQueueId });
    for (const [, placement] of dueItems) ordered.push(placement);
    for (const [, placement] of newItems) ordered.push(placement);

    all(
      this.sql.exec(
        `UPDATE study_queue
         SET position = position + ${ROLLOVER_OFFSET},
             status = CASE WHEN status = 'current' THEN 'queued' ELSE status END
         WHERE session_id = ? AND status IN ('current', 'queued')`,
        session.id,
      ),
    );
    ordered.forEach((placement, index) => {
      const position = index + 1;
      const status = position === 1 ? StudyQueueStatus.CURRENT : StudyQueueStatus.QUEUED;
      if ("queueId" in placement) {
        all(
          this.sql.exec(
            "UPDATE study_queue SET position = ?, status = ? WHERE id = ?",
            position,
            status,
            placement.queueId,
          ),
        );
        return;
      }
      const introduced = placement.card.state === CardScheduleState.NEW ? today : null;
      all(
        this.sql.exec(
          `INSERT INTO study_queue (session_id, card_id, position, status, introduced_local_date)
           VALUES (?, ?, ?, ?, ?)`,
          session.id,
          placement.card.id,
          position,
          status,
          introduced,
        ),
      );
      if (introduced !== null) {
        all(
          this.sql.exec(
            `UPDATE vocabulary_cards
             SET introduced_local_date = COALESCE(introduced_local_date, ?)
             WHERE id = ?`,
            introduced,
            placement.card.id,
          ),
        );
      }
    });
    all(
      this.sql.exec(
        "UPDATE study_sessions SET local_date = ? WHERE id = ? AND local_date != ?",
        today,
        session.id,
        today,
      ),
    );
  }

  private buildSnapshot(sessionId: number): StudySnapshot {
    const session = oneOrNull(
      this.sql.exec<StudySessionRow>("SELECT * FROM study_sessions WHERE id = ?", sessionId),
    );
    if (session === null) throw new Error("missing study session");
    const queue = all(
      this.sql.exec<QueueCardRow>(
        `SELECT ${QUEUE_CARD_COLUMNS}
         FROM study_queue q JOIN vocabulary_cards c ON c.id = q.card_id
         WHERE q.session_id = ? ORDER BY q.position`,
        sessionId,
      ),
    ).map(queueItemSnapshot);
    const promptRow = oneOrNull(
      this.sql.exec<PromptRow>(
        `SELECT * FROM study_prompts
         WHERE session_id = ? AND status IN ('prepared', 'delivered', 'answered')
         ORDER BY id DESC LIMIT 1`,
        sessionId,
      ),
    );
    // A tail retry is extra practice, never a sixth test question.
    const counted =
      session.mode === StudyMode.REVIEW
        ? queue
        : queue.filter((item) => item.retryOfQueueItemId === null);
    return {
      sessionId: session.id,
      mode: session.mode,
      status: session.status,
      localDate: session.local_date,
      queue,
      currentPrompt: promptRow === null ? null : promptSnapshot(promptRow),
      progress: {
        completed: counted.filter((item) => item.status === StudyQueueStatus.COMPLETED).length,
        total: counted.filter((item) => item.status !== StudyQueueStatus.SKIPPED).length,
      },
    };
  }

  private insertAttempt(
    row: FinalizeRow,
    result: ScheduleTransition,
    rating: ReviewRating,
    isSameSessionRetry: boolean,
  ): number {
    const before = result.before;
    all(
      this.sql.exec(
        `INSERT INTO review_attempts (
           card_id, session_id, queue_item_id, prompt_id, answer_draft_id,
           source, rating, submitted_answer, evaluator_grade, evaluation_feedback, reviewed_at,
           before_state, before_stability, before_difficulty, before_due_at,
           before_effective_due_at, before_last_review_at, before_repetitions, before_lapses,
           after_state, after_stability, after_difficulty, after_raw_due_at,
           after_effective_due_at, after_last_review_at, after_repetitions, after_lapses,
           scheduler_kind, scheduler_version, parameters_version, parameter_fingerprint,
           desired_retention, is_same_session_retry, retry_of_attempt_id, created_at
         ) VALUES (
           ?, ?, ?, ?, ?, 'review', ?, ?, ?, ?, ?,
           ?, ?, ?, ?, ?, ?, ?, ?,
           ?, ?, ?, ?, ?, ?, ?, ?,
           ?, ?, ?, ?, ?, ?, ?, ?
         )`,
        row.card_id,
        row.session_id,
        row.queue_id,
        row.prompt_id,
        row.draft_id,
        rating,
        row.submitted_answer,
        row.evaluator_grade,
        row.evaluation_feedback,
        isoTimestamp(result.reviewedAt),
        before.state,
        before.stability,
        before.difficulty,
        isoTimestamp(before.due),
        row.effective_due_at,
        before.lastReview === null ? null : isoTimestamp(before.lastReview),
        before.repetitions,
        before.lapses,
        result.after.state,
        result.after.stability,
        result.after.difficulty,
        isoTimestamp(result.rawDue),
        isoTimestamp(result.effectiveDue),
        isoTimestamp(result.reviewedAt),
        result.after.repetitions,
        result.after.lapses,
        result.after.schedulerKind,
        result.after.schedulerVersion,
        result.after.parametersVersion,
        result.after.parameterFingerprint,
        result.after.desiredRetention,
        isSameSessionRetry ? 1 : 0,
        row.retry_of_attempt_id,
        isoTimestamp(result.reviewedAt),
      ),
    );
    return this.lastInsertId();
  }
}

type FinalizeRow = CardRow & {
  prompt_id: number;
  prompt_status: StudyPromptStatus;
  session_id: number;
  queue_id: number;
  card_id: number;
  retry_of_queue_item_id: number | null;
  retry_of_attempt_id: number | null;
  draft_id: number | null;
  submitted_answer: string | null;
  evaluator_grade: EvaluationGradeValue | null;
  evaluation_feedback: string | null;
};
