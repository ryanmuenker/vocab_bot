/**
 * Snapshot format v2: the full v5 spaced-review state as a canonical JSON
 * document that both this Worker and `src/hermes_vocab/cloudflare_snapshot.py`
 * can produce and consume byte-identically.
 *
 * The wire domain is deliberately narrow: null, booleans are never used, safe
 * non-negative integers, and strings. SQLite REAL columns (the FSRS scalars)
 * travel as canonical decimal strings because JavaScript renders the double
 * 1.0 as "1" while Python renders it as "1.0"; encoding them as text removes
 * every number-formatting divergence from the digest.
 */

const SHA256_PATTERN = /^[0-9a-f]{64}$/u;
const UTC_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/u;
const DATE = /^\d{4}-\d{2}-\d{2}$/u;
const REAL = /^(?:0|[1-9][0-9]*)\.[0-9]+$/u;

/** Band in which JavaScript and Python both render doubles as plain decimals. */
const REAL_MIN = 1e-4;
const REAL_MAX = 1e16;

const GRADES = ["correct", "partial", "incorrect"] as const;
const CARD_STATES = ["new", "review", "relearning"] as const;

/**
 * Column kinds. `real` values are decimal strings on the wire and doubles in
 * SQLite; every other kind maps straight through.
 */
type Kind =
  | "id"
  | "idNull"
  | "int"
  | "text"
  | "textNull"
  | "prose"
  | "proseNull"
  | "ts"
  | "tsNull"
  | "date"
  | "dateNull"
  | "real"
  | "realNull";

type Column =
  | readonly [json: string, sql: string, kind: Kind]
  | readonly [json: string, sql: string, kind: "enum" | "enumNull", values: readonly string[]];

type ValueOf<C> = C extends readonly [string, string, infer K]
  ? K extends "id" | "int"
    ? number
    : K extends "idNull"
      ? number | null
      : K extends "text" | "prose" | "ts" | "date" | "real"
        ? string
        : string | null
  : C extends readonly [string, string, infer K, infer V extends readonly string[]]
    ? K extends "enum"
      ? V[number]
      : V[number] | null
    : never;

type RowOf<T extends readonly Column[]> = {
  readonly [C in T[number] as C[0]]: ValueOf<C>;
};

const ENTRY_COLUMNS = [
  ["id", "id", "id"],
  ["displayText", "display_text", "text"],
  ["normalizedText", "normalized_text", "text"],
  ["dateAdded", "date_added", "ts"],
  ["lastReviewed", "last_reviewed", "tsNull"],
  ["reviewStatus", "review_status", "enum", ["new", "reviewed"]],
] as const satisfies readonly Column[];

const SENSE_COLUMNS = [
  ["id", "id", "id"],
  ["entryId", "entry_id", "id"],
  ["definition", "definition", "text"],
  ["partOfSpeech", "part_of_speech", "text"],
  ["exampleSentence", "example_sentence", "text"],
  ["sourceContext", "source_context", "textNull"],
  ["dateAdded", "date_added", "ts"],
] as const satisfies readonly Column[];

const CARD_COLUMNS = [
  ["id", "id", "id"],
  ["entryId", "entry_id", "id"],
  ["senseId", "sense_id", "idNull"],
  ["direction", "direction", "enum", ["forward", "reverse"]],
  ["state", "state", "enum", CARD_STATES],
  ["stability", "stability", "realNull"],
  ["difficulty", "difficulty", "realNull"],
  ["dueAt", "due_at", "ts"],
  ["effectiveDueAt", "effective_due_at", "ts"],
  ["lastReviewAt", "last_review_at", "tsNull"],
  ["repetitions", "repetitions", "int"],
  ["lapses", "lapses", "int"],
  ["schedulerKind", "scheduler_kind", "text"],
  ["schedulerVersion", "scheduler_version", "text"],
  ["parametersVersion", "parameters_version", "text"],
  ["parameterFingerprint", "parameter_fingerprint", "text"],
  ["desiredRetention", "desired_retention", "real"],
  ["introducedLocalDate", "introduced_local_date", "dateNull"],
  ["buriedUntilLocalDate", "buried_until_local_date", "dateNull"],
  ["createdAt", "created_at", "ts"],
] as const satisfies readonly Column[];

const STUDY_SESSION_COLUMNS = [
  ["id", "id", "id"],
  ["mode", "mode", "enum", ["review", "test_forward", "test_reverse"]],
  ["status", "status", "enum", ["active", "interrupted", "completed", "exited"]],
  ["startedAt", "started_at", "ts"],
  ["completedAt", "completed_at", "tsNull"],
  ["localDate", "local_date", "date"],
  ["legacyTestSessionId", "legacy_test_session_id", "idNull"],
] as const satisfies readonly Column[];

const STUDY_QUEUE_COLUMNS = [
  ["id", "id", "id"],
  ["sessionId", "session_id", "id"],
  ["cardId", "card_id", "id"],
  ["position", "position", "int"],
  ["status", "status", "enum", ["queued", "current", "completed", "skipped"]],
  ["retryOfQueueItemId", "retry_of_queue_item_id", "idNull"],
  ["completedAttemptId", "completed_attempt_id", "idNull"],
  ["legacyTestQuestionId", "legacy_test_question_id", "idNull"],
  ["introducedLocalDate", "introduced_local_date", "dateNull"],
] as const satisfies readonly Column[];

const STUDY_PROMPT_COLUMNS = [
  ["id", "id", "id"],
  ["sessionId", "session_id", "id"],
  ["queueItemId", "queue_item_id", "id"],
  ["promptKey", "prompt_key", "text"],
  ["promptText", "prompt_text", "prose"],
  [
    "status",
    "status",
    "enum",
    ["prepared", "delivered", "answered", "completed", "failed", "cancelled"],
  ],
  ["preparedAt", "prepared_at", "ts"],
  ["deliveredAt", "delivered_at", "tsNull"],
  ["answeredAt", "answered_at", "tsNull"],
] as const satisfies readonly Column[];

const DELIVERY_ATTEMPT_COLUMNS = [
  ["id", "id", "id"],
  ["promptId", "prompt_id", "id"],
  ["attemptNumber", "attempt_number", "int"],
  ["status", "status", "enum", ["unknown", "failed", "delivered"]],
  ["attemptedAt", "attempted_at", "ts"],
  ["receiptAt", "receipt_at", "tsNull"],
  ["outboundDeliveryId", "outbound_delivery_id", "textNull"],
  ["contentFingerprint", "content_fingerprint", "textNull"],
  ["errorText", "error_text", "textNull"],
] as const satisfies readonly Column[];

const ANSWER_DRAFT_COLUMNS = [
  ["id", "id", "id"],
  ["promptId", "prompt_id", "id"],
  ["submittedAnswer", "submitted_answer", "prose"],
  ["evaluatorGrade", "evaluator_grade", "enum", GRADES],
  ["evaluationFeedback", "evaluation_feedback", "prose"],
  ["answeredAt", "answered_at", "ts"],
  ["createdAt", "created_at", "ts"],
] as const satisfies readonly Column[];

const REVIEW_ATTEMPT_COLUMNS = [
  ["id", "id", "id"],
  ["cardId", "card_id", "id"],
  ["sessionId", "session_id", "idNull"],
  ["queueItemId", "queue_item_id", "idNull"],
  ["promptId", "prompt_id", "idNull"],
  ["answerDraftId", "answer_draft_id", "idNull"],
  ["source", "source", "enum", ["review", "test_forward", "test_reverse", "migration"]],
  ["rating", "rating", "enum", ["again", "hard", "good", "easy"]],
  ["submittedAnswer", "submitted_answer", "proseNull"],
  ["evaluatorGrade", "evaluator_grade", "enumNull", GRADES],
  ["evaluationFeedback", "evaluation_feedback", "proseNull"],
  ["reviewedAt", "reviewed_at", "ts"],
  ["beforeState", "before_state", "enum", CARD_STATES],
  ["beforeStability", "before_stability", "realNull"],
  ["beforeDifficulty", "before_difficulty", "realNull"],
  ["beforeDueAt", "before_due_at", "ts"],
  ["beforeEffectiveDueAt", "before_effective_due_at", "ts"],
  ["beforeLastReviewAt", "before_last_review_at", "tsNull"],
  ["beforeRepetitions", "before_repetitions", "int"],
  ["beforeLapses", "before_lapses", "int"],
  ["afterState", "after_state", "enum", ["review", "relearning"]],
  ["afterStability", "after_stability", "real"],
  ["afterDifficulty", "after_difficulty", "real"],
  ["afterRawDueAt", "after_raw_due_at", "ts"],
  ["afterEffectiveDueAt", "after_effective_due_at", "ts"],
  ["afterLastReviewAt", "after_last_review_at", "ts"],
  ["afterRepetitions", "after_repetitions", "int"],
  ["afterLapses", "after_lapses", "int"],
  ["schedulerKind", "scheduler_kind", "text"],
  ["schedulerVersion", "scheduler_version", "text"],
  ["parametersVersion", "parameters_version", "text"],
  ["parameterFingerprint", "parameter_fingerprint", "text"],
  ["desiredRetention", "desired_retention", "real"],
  ["isSameSessionRetry", "is_same_session_retry", "int"],
  ["retryOfAttemptId", "retry_of_attempt_id", "idNull"],
  ["legacySource", "legacy_source", "textNull"],
  ["legacyId", "legacy_id", "idNull"],
  ["createdAt", "created_at", "ts"],
] as const satisfies readonly Column[];

const REVIEW_EVENT_COLUMNS = [
  ["id", "id", "id"],
  ["entryId", "entry_id", "id"],
  ["reviewDate", "review_date", "date"],
  ["status", "status", "enum", ["pending", "answered", "missed"]],
  ["promptedAt", "prompted_at", "ts"],
  ["answeredAt", "answered_at", "tsNull"],
  ["answerText", "answer_text", "textNull"],
  ["grade", "grade", "enumNull", GRADES],
  ["evaluationFeedback", "evaluation_feedback", "proseNull"],
] as const satisfies readonly Column[];

const TEST_SESSION_COLUMNS = [
  ["id", "id", "id"],
  ["status", "status", "enum", ["active", "completed"]],
  ["startedAt", "started_at", "ts"],
  ["completedAt", "completed_at", "tsNull"],
] as const satisfies readonly Column[];

const TEST_QUESTION_COLUMNS = [
  ["id", "id", "id"],
  ["sessionId", "session_id", "id"],
  ["entryId", "entry_id", "id"],
  ["position", "position", "int"],
  ["answerText", "answer_text", "proseNull"],
  ["grade", "grade", "enumNull", GRADES],
  ["evaluationFeedback", "evaluation_feedback", "proseNull"],
  ["answeredAt", "answered_at", "tsNull"],
] as const satisfies readonly Column[];

export type SnapshotEntry = RowOf<typeof ENTRY_COLUMNS>;
export type SnapshotSense = RowOf<typeof SENSE_COLUMNS>;
export type SnapshotCard = RowOf<typeof CARD_COLUMNS>;
export type SnapshotStudySession = RowOf<typeof STUDY_SESSION_COLUMNS>;
export type SnapshotStudyQueueItem = RowOf<typeof STUDY_QUEUE_COLUMNS>;
export type SnapshotStudyPrompt = RowOf<typeof STUDY_PROMPT_COLUMNS>;
export type SnapshotDeliveryAttempt = RowOf<typeof DELIVERY_ATTEMPT_COLUMNS>;
export type SnapshotAnswerDraft = RowOf<typeof ANSWER_DRAFT_COLUMNS>;
export type SnapshotReviewAttempt = RowOf<typeof REVIEW_ATTEMPT_COLUMNS>;
export type SnapshotReviewEvent = RowOf<typeof REVIEW_EVENT_COLUMNS>;
export type SnapshotTestSession = RowOf<typeof TEST_SESSION_COLUMNS>;
export type SnapshotTestQuestion = RowOf<typeof TEST_QUESTION_COLUMNS>;

/**
 * Tables in dependency order: importing top to bottom satisfies every foreign
 * key except the study_queue <-> review_attempts cycle, which needs deferral.
 */
const TABLES = [
  { key: "entries", table: "vocabulary_entries", columns: ENTRY_COLUMNS },
  { key: "senses", table: "vocabulary_senses", columns: SENSE_COLUMNS },
  { key: "reviewEvents", table: "review_events", columns: REVIEW_EVENT_COLUMNS },
  { key: "testSessions", table: "test_sessions", columns: TEST_SESSION_COLUMNS },
  { key: "testQuestions", table: "test_questions", columns: TEST_QUESTION_COLUMNS },
  { key: "cards", table: "vocabulary_cards", columns: CARD_COLUMNS },
  { key: "studySessions", table: "study_sessions", columns: STUDY_SESSION_COLUMNS },
  { key: "studyQueue", table: "study_queue", columns: STUDY_QUEUE_COLUMNS },
  { key: "studyPrompts", table: "study_prompts", columns: STUDY_PROMPT_COLUMNS },
  { key: "deliveryAttempts", table: "prompt_delivery_attempts", columns: DELIVERY_ATTEMPT_COLUMNS },
  { key: "answerDrafts", table: "answer_drafts", columns: ANSWER_DRAFT_COLUMNS },
  { key: "reviewAttempts", table: "review_attempts", columns: REVIEW_ATTEMPT_COLUMNS },
] as const satisfies readonly { key: string; table: string; columns: readonly Column[] }[];

export interface SnapshotV2 {
  readonly formatVersion: 2;
  readonly entries: readonly SnapshotEntry[];
  readonly senses: readonly SnapshotSense[];
  readonly reviewEvents: readonly SnapshotReviewEvent[];
  readonly testSessions: readonly SnapshotTestSession[];
  readonly testQuestions: readonly SnapshotTestQuestion[];
  readonly cards: readonly SnapshotCard[];
  readonly studySessions: readonly SnapshotStudySession[];
  readonly studyQueue: readonly SnapshotStudyQueueItem[];
  readonly studyPrompts: readonly SnapshotStudyPrompt[];
  readonly deliveryAttempts: readonly SnapshotDeliveryAttempt[];
  readonly answerDrafts: readonly SnapshotAnswerDraft[];
  readonly reviewAttempts: readonly SnapshotReviewAttempt[];
}

export interface ExportEnvelope {
  readonly sha256: string;
  readonly snapshot: SnapshotV2;
}

export type SnapshotSummary = { readonly [K in (typeof TABLES)[number]["key"]]: number } & {
  readonly sha256: string;
};

/**
 * Render a double as the canonical decimal string shared with Python's
 * `repr`. Both runtimes emit shortest round-tripping digits, so restricting
 * the magnitude to the band where neither switches to exponent notation makes
 * the two renderings identical.
 */
export function encodeReal(value: number): string {
  if (
    !Number.isFinite(value) ||
    value < 0 ||
    Object.is(value, -0) ||
    (value !== 0 && (value < REAL_MIN || value >= REAL_MAX))
  ) {
    throw new TypeError("Real outside the snapshot domain");
  }
  const rendered = Number.isInteger(value) ? `${value}.0` : `${value}`;
  if (!REAL.test(rendered)) throw new TypeError("Real is not a canonical decimal");
  return rendered;
}

function decodeReal(value: string): number {
  const parsed = Number(value);
  if (encodeReal(parsed) !== value) throw new TypeError("Real is not a canonical decimal");
  return parsed;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function integer(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function validUnicode(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (index + 1 >= value.length || next < 0xdc00 || next > 0xdfff) return false;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return false;
    }
  }
  return true;
}

function validColumn(value: unknown, column: Column): boolean {
  const kind = column[2];
  if (value === null) {
    return (
      kind === "idNull" ||
      kind === "textNull" ||
      kind === "proseNull" ||
      kind === "tsNull" ||
      kind === "dateNull" ||
      kind === "realNull" ||
      kind === "enumNull"
    );
  }
  switch (kind) {
    case "id":
    case "idNull":
    case "int":
      return integer(value);
    case "enum":
    case "enumNull":
      return typeof value === "string" && column[3].includes(value);
    default:
      break;
  }
  if (typeof value !== "string" || value.length === 0 || !validUnicode(value)) return false;
  switch (kind) {
    case "text":
    case "textNull":
      return true;
    case "prose":
    case "proseNull":
      // Mirrors SQLite's `length(trim(x)) > 0`, which strips spaces only.
      return /[^ ]/u.test(value);
    case "ts":
    case "tsNull":
      return UTC_TIMESTAMP.test(value);
    case "date":
    case "dateNull":
      return DATE.test(value);
    default:
      if (!REAL.test(value)) return false;
      try {
        decodeReal(value);
        return true;
      } catch {
        return false;
      }
  }
}

function parseTable(value: unknown, columns: readonly Column[]): Record<string, unknown>[] | null {
  if (!Array.isArray(value)) return null;
  const names = columns.map(([json]) => json);
  const rows: Record<string, unknown>[] = [];
  let previous = -1;
  for (const item of value) {
    const row = record(item);
    if (row === null || !exactKeys(row, names)) return null;
    for (const column of columns) {
      if (!validColumn(row[column[0]], column)) return null;
    }
    const { id } = row;
    if (typeof id !== "number" || id <= previous) return null;
    previous = id;
    rows.push(row);
  }
  return rows;
}

function ids(rows: readonly { readonly id: number }[]): Set<number> {
  return new Set(rows.map(({ id }) => id));
}

function uniqueNonNull(values: readonly (string | number | null)[]): boolean {
  const present = values.filter((value) => value !== null);
  return new Set(present).size === present.length;
}

/** Cross-row invariants that the SQL CHECK constraints and indexes enforce. */
function validSemantics(snapshot: SnapshotV2): boolean {
  const entryIds = ids(snapshot.entries);
  const senseIds = ids(snapshot.senses);
  const cardIds = ids(snapshot.cards);
  const studySessionIds = ids(snapshot.studySessions);
  const queueIds = ids(snapshot.studyQueue);
  const promptIds = ids(snapshot.studyPrompts);
  const draftIds = ids(snapshot.answerDrafts);
  const attemptIds = ids(snapshot.reviewAttempts);
  const testSessionIds = ids(snapshot.testSessions);
  const testQuestionIds = ids(snapshot.testQuestions);
  const senseEntry = new Map(snapshot.senses.map(({ id, entryId }) => [id, entryId]));

  if (!uniqueNonNull(snapshot.entries.map(({ normalizedText }) => normalizedText))) return false;
  if (snapshot.senses.some(({ entryId }) => !entryIds.has(entryId))) return false;

  // Legacy audit carriers keep their pre-card shape. An answered event must
  // carry the answer itself, but `grade`/`evaluationFeedback` may be null:
  // v4 recorded answers before it recorded grades, and those rows are real
  // history. The v5 backfill already ignores ungraded events for scheduling.
  for (const event of snapshot.reviewEvents) {
    if (!entryIds.has(event.entryId)) return false;
    const answered = event.status === "answered";
    if ((event.answeredAt !== null) !== answered) return false;
    if ((event.answerText !== null) !== answered) return false;
    if (!answered && (event.grade !== null || event.evaluationFeedback !== null)) return false;
  }
  if (!uniqueNonNull(snapshot.reviewEvents.map(({ reviewDate }) => reviewDate))) return false;
  if (snapshot.testSessions.some(({ status, completedAt }) => (status === "active") === (completedAt !== null))) {
    return false;
  }
  if (snapshot.testSessions.filter(({ status }) => status === "active").length > 1) return false;
  const questionPositions: string[] = [];
  const questionEntries: string[] = [];
  for (const question of snapshot.testQuestions) {
    if (!entryIds.has(question.entryId) || !testSessionIds.has(question.sessionId)) return false;
    if (question.position < 1 || question.position > 5) return false;
    questionPositions.push(`${question.sessionId}:${question.position}`);
    questionEntries.push(`${question.sessionId}:${question.entryId}`);
    const fields = [
      question.answerText,
      question.grade,
      question.evaluationFeedback,
      question.answeredAt,
    ];
    if (fields.some((field) => field !== null) && fields.some((field) => field === null)) return false;
  }
  if (!uniqueNonNull(questionPositions) || !uniqueNonNull(questionEntries)) return false;

  const forwardEntries: number[] = [];
  const reverseSenses: number[] = [];
  for (const card of snapshot.cards) {
    if (!entryIds.has(card.entryId)) return false;
    if (card.direction === "forward") {
      if (card.senseId !== null) return false;
      forwardEntries.push(card.entryId);
    } else {
      if (card.senseId === null || senseEntry.get(card.senseId) !== card.entryId) return false;
      reverseSenses.push(card.senseId);
    }
    const stability = card.stability === null ? null : decodeReal(card.stability);
    const difficulty = card.difficulty === null ? null : decodeReal(card.difficulty);
    const fresh =
      card.state === "new" &&
      stability === null &&
      difficulty === null &&
      card.lastReviewAt === null &&
      card.repetitions === 0 &&
      card.lapses === 0;
    const seen =
      card.state !== "new" &&
      stability !== null &&
      stability > 0 &&
      difficulty !== null &&
      difficulty >= 1 &&
      difficulty <= 10 &&
      card.lastReviewAt !== null &&
      card.repetitions >= 1;
    if (!fresh && !seen) return false;
    if (card.lapses > card.repetitions) return false;
    const retention = decodeReal(card.desiredRetention);
    if (retention <= 0 || retention >= 1) return false;
  }
  if (!uniqueNonNull(forwardEntries) || !uniqueNonNull(reverseSenses)) return false;
  if (snapshot.cards.some(({ senseId }) => senseId !== null && !senseIds.has(senseId))) return false;

  let openSessions = 0;
  for (const session of snapshot.studySessions) {
    const open = session.status === "active" || session.status === "interrupted";
    if (open === (session.completedAt !== null)) return false;
    if (open) openSessions += 1;
    if (session.legacyTestSessionId !== null && !testSessionIds.has(session.legacyTestSessionId)) {
      return false;
    }
  }
  if (openSessions > 1) return false;
  if (!uniqueNonNull(snapshot.studySessions.map(({ legacyTestSessionId }) => legacyTestSessionId))) {
    return false;
  }

  const queuePositions: string[] = [];
  const currentSessions: number[] = [];
  for (const item of snapshot.studyQueue) {
    if (!studySessionIds.has(item.sessionId) || !cardIds.has(item.cardId)) return false;
    if (item.position < 1) return false;
    queuePositions.push(`${item.sessionId}:${item.position}`);
    if (item.status === "current") currentSessions.push(item.sessionId);
    if (item.retryOfQueueItemId !== null) {
      if (item.retryOfQueueItemId === item.id || !queueIds.has(item.retryOfQueueItemId)) return false;
    }
    if ((item.status === "completed") !== (item.completedAttemptId !== null)) return false;
    if (item.completedAttemptId !== null && !attemptIds.has(item.completedAttemptId)) return false;
    if (item.legacyTestQuestionId !== null && !testQuestionIds.has(item.legacyTestQuestionId)) {
      return false;
    }
  }
  if (!uniqueNonNull(queuePositions) || !uniqueNonNull(currentSessions)) return false;
  if (!uniqueNonNull(snapshot.studyQueue.map(({ retryOfQueueItemId }) => retryOfQueueItemId))) return false;
  if (!uniqueNonNull(snapshot.studyQueue.map(({ completedAttemptId }) => completedAttemptId))) return false;
  if (!uniqueNonNull(snapshot.studyQueue.map(({ legacyTestQuestionId }) => legacyTestQuestionId))) {
    return false;
  }

  let activePrompts = 0;
  for (const prompt of snapshot.studyPrompts) {
    if (!studySessionIds.has(prompt.sessionId) || !queueIds.has(prompt.queueItemId)) return false;
    const delivered = prompt.status === "delivered" || prompt.status === "answered" || prompt.status === "completed";
    const answered = prompt.status === "answered" || prompt.status === "completed";
    if (prompt.status === "prepared" && prompt.deliveredAt !== null) return false;
    if (delivered && prompt.deliveredAt === null) return false;
    if (answered && prompt.answeredAt === null) return false;
    if (prompt.status === "prepared" || prompt.status === "delivered" || prompt.status === "answered") {
      activePrompts += 1;
    }
  }
  if (activePrompts > 1) return false;
  if (!uniqueNonNull(snapshot.studyPrompts.map(({ promptKey }) => promptKey))) return false;
  if (!uniqueNonNull(snapshot.studyPrompts.map(({ queueItemId }) => queueItemId))) return false;

  const attemptNumbers: string[] = [];
  for (const attempt of snapshot.deliveryAttempts) {
    if (!promptIds.has(attempt.promptId) || attempt.attemptNumber < 1) return false;
    attemptNumbers.push(`${attempt.promptId}:${attempt.attemptNumber}`);
    if (
      attempt.status === "delivered" &&
      (attempt.receiptAt === null || attempt.outboundDeliveryId === null)
    ) return false;
  }
  if (!uniqueNonNull(attemptNumbers)) return false;

  if (snapshot.answerDrafts.some(({ promptId }) => !promptIds.has(promptId))) return false;
  if (!uniqueNonNull(snapshot.answerDrafts.map(({ promptId }) => promptId))) return false;

  const legacyKeys: string[] = [];
  for (const attempt of snapshot.reviewAttempts) {
    if (!cardIds.has(attempt.cardId)) return false;
    if (attempt.sessionId !== null && !studySessionIds.has(attempt.sessionId)) return false;
    if (attempt.queueItemId !== null && !queueIds.has(attempt.queueItemId)) return false;
    if (attempt.promptId !== null && !promptIds.has(attempt.promptId)) return false;
    if (attempt.answerDraftId !== null && !draftIds.has(attempt.answerDraftId)) return false;
    if (attempt.retryOfAttemptId !== null) {
      if (attempt.retryOfAttemptId === attempt.id || !attemptIds.has(attempt.retryOfAttemptId)) {
        return false;
      }
    }
    if ((attempt.legacySource === null) !== (attempt.legacyId === null)) return false;
    if (attempt.legacySource !== null) legacyKeys.push(`${attempt.legacySource}:${attempt.legacyId}`);
    if (attempt.afterRepetitions !== attempt.beforeRepetitions + 1) return false;
    if (attempt.afterLapses > attempt.afterRepetitions || attempt.afterRepetitions < 1) return false;
    if (attempt.isSameSessionRetry !== 0 && attempt.isSameSessionRetry !== 1) return false;
    const stability = decodeReal(attempt.afterStability);
    const difficulty = decodeReal(attempt.afterDifficulty);
    const retention = decodeReal(attempt.desiredRetention);
    if (stability <= 0 || difficulty < 1 || difficulty > 10) return false;
    if (retention <= 0 || retention >= 1) return false;
  }
  if (!uniqueNonNull(legacyKeys)) return false;
  if (!uniqueNonNull(snapshot.reviewAttempts.map(({ promptId }) => promptId))) return false;
  if (!uniqueNonNull(snapshot.reviewAttempts.map(({ retryOfAttemptId }) => retryOfAttemptId))) return false;

  return true;
}

export function parseSnapshot(value: unknown): SnapshotV2 | null {
  const root = record(value);
  const keys = ["formatVersion", ...TABLES.map(({ key }) => key)];
  if (root === null || !exactKeys(root, keys) || root.formatVersion !== 2) return null;
  const parsed: Record<string, unknown> = { formatVersion: 2 };
  for (const { key, columns } of TABLES) {
    const rows = parseTable(root[key], columns);
    if (rows === null) return null;
    parsed[key] = rows;
  }
  const snapshot = parsed as unknown as SnapshotV2;
  try {
    return validSemantics(snapshot) ? snapshot : null;
  } catch {
    return null;
  }
}

export function canonicalizeJcs(value: unknown): string {
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value < 0) throw new TypeError("JCS number outside snapshot domain");
    return JSON.stringify(value);
  }
  if (typeof value === "string") {
    if (!validUnicode(value)) throw new TypeError("JCS string contains a lone surrogate");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalizeJcs).join(",")}]`;
  const object = record(value);
  if (object === null) throw new TypeError("Unsupported JCS value");
  return `{${Object.keys(object).sort().map((key) => {
    if (!validUnicode(key)) throw new TypeError("JCS key contains a lone surrogate");
    return `${JSON.stringify(key)}:${canonicalizeJcs(object[key])}`;
  }).join(",")}}`;
}

export async function sha256Snapshot(snapshot: SnapshotV2): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonicalizeJcs(snapshot)),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Parse an export envelope and reject it unless the embedded digest matches a
 * freshly computed one. There is deliberately no unverified variant.
 */
export async function parseVerifiedEnvelope(value: unknown): Promise<ExportEnvelope | null> {
  const root = record(value);
  if (root === null || !exactKeys(root, ["sha256", "snapshot"]) ||
      typeof root.sha256 !== "string" || !SHA256_PATTERN.test(root.sha256)) return null;
  const snapshot = parseSnapshot(root.snapshot);
  if (snapshot === null || (await sha256Snapshot(snapshot)) !== root.sha256) return null;
  return { sha256: root.sha256, snapshot };
}

export function summarizeSnapshot(snapshot: SnapshotV2, sha256: string): SnapshotSummary {
  const counts: Record<string, number> = {};
  for (const { key } of TABLES) counts[key] = snapshot[key].length;
  return { ...counts, sha256 } as SnapshotSummary;
}

/** Read the whole v5 state out of Durable Object SQLite. */
export function readSnapshot(storage: DurableObjectStorage): SnapshotV2 {
  const snapshot: Record<string, unknown> = { formatVersion: 2 };
  for (const { key, table, columns } of TABLES) {
    const query = `SELECT ${columns.map(([, column]) => column).join(", ")} FROM ${table} ORDER BY id`;
    snapshot[key] = Array.from(
      storage.sql.exec<Record<string, string | number | null>>(query),
    ).map((raw) => {
      const row: Record<string, unknown> = {};
      for (const [json, column, kind] of columns) {
        const value = raw[column] ?? null;
        row[json] =
          typeof value === "number" && (kind === "real" || kind === "realNull")
            ? encodeReal(value)
            : value;
      }
      return row;
    });
  }
  const parsed = parseSnapshot(snapshot);
  if (parsed === null) throw new TypeError("Stored state is not a valid SnapshotV2");
  return parsed;
}

/** Write a validated snapshot into otherwise empty Durable Object SQLite. */
export function writeSnapshot(storage: DurableObjectStorage, snapshot: SnapshotV2): void {
  storage.transactionSync(() => {
    const sql = storage.sql;
    // study_queue.completed_attempt_id and review_attempts.queue_item_id form
    // a cycle, so no insert order satisfies both. parseSnapshot already proved
    // every reference resolves, leaving the commit-time check to confirm it.
    Array.from(sql.exec("PRAGMA defer_foreign_keys = ON"));
    for (const { table } of TABLES) {
      if (sql.exec<{ count: number }>(`SELECT COUNT(*) AS count FROM ${table}`).one().count !== 0) {
        throw new Error("snapshot import requires empty storage");
      }
    }
    for (const { key, table, columns } of TABLES) {
      const query =
        `INSERT INTO ${table} (${columns.map(([, column]) => column).join(", ")}) ` +
        `VALUES (${columns.map(() => "?").join(", ")})`;
      // Rows are validated SnapshotV2 rows; the union of per-table row shapes
      // has no index signature, so widen once here rather than per access.
      const rows = snapshot[key] as readonly Record<string, string | number | null>[];
      for (const row of rows) {
        Array.from(sql.exec(
          query,
          ...columns.map(([json, , kind]) => {
            const value = row[json] ?? null;
            return typeof value === "string" && (kind === "real" || kind === "realNull")
              ? decodeReal(value)
              : value;
          }),
        ));
      }
    }
  });
}
