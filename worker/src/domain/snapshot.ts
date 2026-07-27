import { EvaluationGrade, TestSessionStatus } from "./models";

export interface SnapshotEntry {
  readonly id: number;
  readonly displayText: string;
  readonly normalizedText: string;
  readonly dateAdded: string;
  readonly lastReviewed: string | null;
  readonly reviewStatus: "new" | "reviewed";
}

export interface SnapshotSense {
  readonly id: number;
  readonly entryId: number;
  readonly definition: string;
  readonly partOfSpeech: string;
  readonly exampleSentence: string;
  readonly sourceContext: string | null;
  readonly dateAdded: string;
}

export interface SnapshotReviewEvent {
  readonly id: number;
  readonly entryId: number;
  readonly reviewDate: string;
  readonly status: "pending" | "answered" | "missed";
  readonly promptedAt: string;
  readonly answeredAt: string | null;
  readonly answerText: string | null;
  readonly grade: "correct" | "partial" | "incorrect" | null;
  readonly evaluationFeedback: string | null;
}

export interface SnapshotTestSession {
  readonly id: number;
  readonly status: "active" | "completed";
  readonly startedAt: string;
  readonly completedAt: string | null;
}

export interface SnapshotTestQuestion {
  readonly id: number;
  readonly sessionId: number;
  readonly entryId: number;
  readonly position: number;
  readonly answerText: string | null;
  readonly grade: "correct" | "partial" | "incorrect" | null;
  readonly evaluationFeedback: string | null;
  readonly answeredAt: string | null;
}

export interface SnapshotV1 {
  readonly formatVersion: 1;
  readonly entries: readonly SnapshotEntry[];
  readonly senses: readonly SnapshotSense[];
  readonly reviewEvents: readonly SnapshotReviewEvent[];
  readonly testSessions: readonly SnapshotTestSession[];
  readonly testQuestions: readonly SnapshotTestQuestion[];
}

export interface ExportEnvelope {
  readonly sha256: string;
  readonly snapshot: SnapshotV1;
}

export interface SnapshotSummary {
  readonly entries: number;
  readonly senses: number;
  readonly reviewEvents: number;
  readonly testSessions: number;
  readonly testQuestions: number;
  readonly sha256: string;
}

const GRADE: Record<string, true> = {
  [EvaluationGrade.CORRECT]: true,
  [EvaluationGrade.PARTIAL]: true,
  [EvaluationGrade.INCORRECT]: true,
};
const REVIEW_STATUS: Record<string, true> = { new: true, reviewed: true };
const EVENT_STATUS: Record<string, true> = { pending: true, answered: true, missed: true };
const SESSION_STATUS: Record<string, true> = {
  [TestSessionStatus.ACTIVE]: true,
  [TestSessionStatus.COMPLETED]: true,
};
const SHA256_PATTERN = /^[0-9a-f]{64}$/u;
const UTC_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/u;
const DATE = /^\d{4}-\d{2}-\d{2}$/u;

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

function text(value: unknown, nullable = false): value is string | null {
  return (
    (nullable && value === null) ||
    (typeof value === "string" && value.length > 0 && validUnicode(value))
  );
}

function timestamp(value: unknown, nullable = false): value is string | null {
  return text(value, nullable) && (value === null || UTC_TIMESTAMP.test(value));
}

function ascendingUniqueIds(rows: readonly { readonly id: number }[]): boolean {
  return rows.every((row, index) => index === 0 || rows[index - 1]!.id < row.id);
}

function parseRows<T>(
  value: unknown,
  parser: (candidate: Record<string, unknown>) => T | null,
): T[] | null {
  if (!Array.isArray(value)) return null;
  const rows: T[] = [];
  for (const item of value) {
    const candidate = record(item);
    if (candidate === null) return null;
    const parsed = parser(candidate);
    if (parsed === null) return null;
    rows.push(parsed);
  }
  return rows;
}

export function parseSnapshot(value: unknown): SnapshotV1 | null {
  const root = record(value);
  if (
    root === null ||
    !exactKeys(root, [
      "formatVersion",
      "entries",
      "senses",
      "reviewEvents",
      "testSessions",
      "testQuestions",
    ]) ||
    root.formatVersion !== 1
  ) return null;

  const entries = parseRows<SnapshotEntry>(root.entries, (row) => {
    if (
      !exactKeys(row, ["id", "displayText", "normalizedText", "dateAdded", "lastReviewed", "reviewStatus"]) ||
      !integer(row.id) ||
      !text(row.displayText) ||
      !text(row.normalizedText) ||
      !timestamp(row.dateAdded) ||
      !timestamp(row.lastReviewed, true) ||
      typeof row.reviewStatus !== "string" ||
      !Object.hasOwn(REVIEW_STATUS, row.reviewStatus)
    ) return null;
    return row as unknown as SnapshotEntry;
  });
  const senses = parseRows<SnapshotSense>(root.senses, (row) => {
    if (
      !exactKeys(row, ["id", "entryId", "definition", "partOfSpeech", "exampleSentence", "sourceContext", "dateAdded"]) ||
      !integer(row.id) || !integer(row.entryId) || !text(row.definition) || !text(row.partOfSpeech) ||
      !text(row.exampleSentence) || !text(row.sourceContext, true) || !timestamp(row.dateAdded)
    ) return null;
    return row as unknown as SnapshotSense;
  });
  const reviewEvents = parseRows<SnapshotReviewEvent>(root.reviewEvents, (row) => {
    if (
      !exactKeys(row, ["id", "entryId", "reviewDate", "status", "promptedAt", "answeredAt", "answerText", "grade", "evaluationFeedback"]) ||
      !integer(row.id) || !integer(row.entryId) || typeof row.reviewDate !== "string" || !DATE.test(row.reviewDate) ||
      typeof row.status !== "string" || !Object.hasOwn(EVENT_STATUS, row.status) || !timestamp(row.promptedAt) ||
      !timestamp(row.answeredAt, true) || !text(row.answerText, true) ||
      !(row.grade === null || (typeof row.grade === "string" && Object.hasOwn(GRADE, row.grade))) ||
      !text(row.evaluationFeedback, true)
    ) return null;
    if (
      (row.status === "pending" && (row.answeredAt !== null || row.answerText !== null || row.grade !== null || row.evaluationFeedback !== null)) ||
      (row.status === "answered" && (row.answeredAt === null || row.answerText === null || row.grade === null || row.evaluationFeedback === null)) ||
      (row.status === "missed" && (row.answeredAt !== null || row.answerText !== null || row.grade !== null || row.evaluationFeedback !== null))
    ) return null;
    return row as unknown as SnapshotReviewEvent;
  });
  const testSessions = parseRows<SnapshotTestSession>(root.testSessions, (row) => {
    if (
      !exactKeys(row, ["id", "status", "startedAt", "completedAt"]) || !integer(row.id) ||
      typeof row.status !== "string" || !Object.hasOwn(SESSION_STATUS, row.status) ||
      !timestamp(row.startedAt) || !timestamp(row.completedAt, true) ||
      (row.status === "active") === (row.completedAt !== null)
    ) return null;
    return row as unknown as SnapshotTestSession;
  });
  const testQuestions = parseRows<SnapshotTestQuestion>(root.testQuestions, (row) => {
    if (
      !exactKeys(row, ["id", "sessionId", "entryId", "position", "answerText", "grade", "evaluationFeedback", "answeredAt"]) ||
      !integer(row.id) || !integer(row.sessionId) || !integer(row.entryId) || !integer(row.position) ||
      row.position < 1 || row.position > 5 || !text(row.answerText, true) ||
      !(row.grade === null || (typeof row.grade === "string" && Object.hasOwn(GRADE, row.grade))) ||
      !text(row.evaluationFeedback, true) || !timestamp(row.answeredAt, true)
    ) return null;
    const pending = row.answerText === null && row.grade === null && row.evaluationFeedback === null && row.answeredAt === null;
    const answered = row.answerText !== null && row.grade !== null && row.evaluationFeedback !== null && row.answeredAt !== null;
    return pending || answered ? (row as unknown as SnapshotTestQuestion) : null;
  });
  if ([entries, senses, reviewEvents, testSessions, testQuestions].some((rows) => rows === null)) return null;
  const snapshot = { formatVersion: 1, entries, senses, reviewEvents, testSessions, testQuestions } as SnapshotV1;
  if (!ascendingUniqueIds(entries!) || !ascendingUniqueIds(senses!) || !ascendingUniqueIds(reviewEvents!) ||
      !ascendingUniqueIds(testSessions!) || !ascendingUniqueIds(testQuestions!)) return null;

  const entryIds = new Set(entries!.map(({ id }) => id));
  const sessionIds = new Set(testSessions!.map(({ id }) => id));
  if (senses!.some(({ entryId }) => !entryIds.has(entryId)) || reviewEvents!.some(({ entryId }) => !entryIds.has(entryId)) ||
      testQuestions!.some(({ entryId, sessionId }) => !entryIds.has(entryId) || !sessionIds.has(sessionId))) return null;
  if (new Set(reviewEvents!.map(({ reviewDate }) => reviewDate)).size !== reviewEvents!.length) return null;
  if (testSessions!.filter(({ status }) => status === "active").length > 1) return null;
  const sessionPosition = new Set<string>();
  const sessionEntry = new Set<string>();
  for (const question of testQuestions!) {
    const positionKey = `${question.sessionId}:${question.position}`;
    const entryKey = `${question.sessionId}:${question.entryId}`;
    if (sessionPosition.has(positionKey) || sessionEntry.has(entryKey)) return null;
    sessionPosition.add(positionKey);
    sessionEntry.add(entryKey);
  }
  return snapshot;
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

export async function sha256Snapshot(snapshot: SnapshotV1): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonicalizeJcs(snapshot)),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function parseExportEnvelope(value: unknown): ExportEnvelope | null {
  const root = record(value);
  if (root === null || !exactKeys(root, ["sha256", "snapshot"]) ||
      typeof root.sha256 !== "string" || !SHA256_PATTERN.test(root.sha256)) return null;
  const snapshot = parseSnapshot(root.snapshot);
  return snapshot === null ? null : { sha256: root.sha256, snapshot };
}
