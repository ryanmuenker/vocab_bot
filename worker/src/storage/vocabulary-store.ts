import {
  CaptureStatus,
  EntryTextStatus,
  EvaluationGrade,
  PendingReviewStatus,
  ReviewCompletionStatus,
  ReviewPromptStatus,
  TestCompletionStatus,
  TestSessionStatus,
  TestSnapshotStatus,
  TestStartStatus,
} from "../domain/models";
import type {
  EntryCaptureResult,
  Evaluation,
  EvaluationGrade as EvaluationGradeValue,
  PendingReviewResult,
  ReviewCompletionResult,
  ReviewEvent,
  ReviewPromptResult,
  SenseCard,
  TestCompletionResult,
  TestQuestion,
  TestSession,
  TestSessionSnapshot,
  TestSnapshotResult,
  TestStartResult,
  VocabularyEntry,
  VocabularySense,
} from "../domain/models";
import { normalizeEntryText, trimPythonWhitespace, validateSenseCards } from "../domain/normalization";
import type { SnapshotV1 } from "../domain/snapshot";

const REQUIRED_TEST_QUESTIONS = 5;
const VALID_GRADES: Record<EvaluationGradeValue, true> = {
  [EvaluationGrade.CORRECT]: true,
  [EvaluationGrade.PARTIAL]: true,
  [EvaluationGrade.INCORRECT]: true,
};

class ReviewCasLost extends Error {}
class TestCasLost extends Error {}

type SqlRow = Record<string, SqlStorageValue>;

function all<T extends SqlRow>(cursor: SqlStorageCursor<T>): T[] {
  return Array.from(cursor);
}

function oneOrNull<T extends SqlRow>(cursor: SqlStorageCursor<T>): T | null {
  const values = all(cursor);
  return values[0] ?? null;
}

function isoTimestamp(now: Date): string {
  return now.toISOString().replace(/\.000Z$/, "Z");
}

function localDate(now: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function isEvaluation(value: Evaluation | null): value is Evaluation {
  return (
    value !== null &&
    Object.hasOwn(VALID_GRADES, value.grade) &&
    typeof value.feedback === "string" &&
    value.feedback.trim().length > 0
  );
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

interface ReviewRow extends SqlRow {
  id: number;
  entry_id: number;
  review_date: string;
  status: "pending" | "answered" | "missed";
  prompted_at: string;
  answered_at: string | null;
  answer_text: string | null;
  grade: EvaluationGradeValue | null;
  evaluation_feedback: string | null;
}

interface SessionRow extends SqlRow {
  id: number;
  status: "active" | "completed";
  started_at: string;
  completed_at: string | null;
}

interface QuestionRow extends SqlRow {
  id: number;
  session_id: number;
  entry_id: number;
  position: number;
  answer_text: string | null;
  grade: EvaluationGradeValue | null;
  evaluation_feedback: string | null;
  answered_at: string | null;
}

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

function eventFromRow(row: ReviewRow): ReviewEvent {
  return {
    id: row.id,
    entryId: row.entry_id,
    reviewDate: row.review_date,
    status: row.status,
    promptedAt: row.prompted_at,
    answeredAt: row.answered_at,
    answerText: row.answer_text,
    grade: row.grade,
    feedback: row.evaluation_feedback,
  };
}

function sessionFromRow(row: SessionRow): TestSession {
  return {
    id: row.id,
    status: row.status === "active" ? TestSessionStatus.ACTIVE : TestSessionStatus.COMPLETED,
    startedAt: row.started_at,
    completedAt: row.completed_at,
  };
}

export class VocabularyStore {
  constructor(
    private readonly storage: DurableObjectStorage,
    private readonly timeZone = "Asia/Kuala_Lumpur",
  ) {}

  private get sql(): SqlStorage {
    return this.storage.sql;
  }

  private loadEntryById(id: number): VocabularyEntry | null {
    const row = oneOrNull(this.sql.exec<EntryRow>("SELECT * FROM vocabulary_entries WHERE id = ?", id));
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

  getEntry(text: string): VocabularyEntry | null {
    const normalized = normalizeEntryText(text);
    return normalized.status === EntryTextStatus.VALID
      ? this.loadEntryByNormalized(normalized.normalizedText)
      : null;
  }

  captureEntry(displayText: string, cards: readonly SenseCard[], now = new Date()): EntryCaptureResult {
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
        const entryId = oneOrNull(
          this.sql.exec<{ id: number }>("SELECT last_insert_rowid() AS id"),
        )?.id;
        if (entryId === undefined) throw new Error("missing entry id");
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

  pendingReview(): PendingReviewResult {
    try {
      const row = oneOrNull(
        this.sql.exec<ReviewRow>(
          `SELECT * FROM review_events
           WHERE status = 'pending'
           ORDER BY review_date DESC, id DESC
           LIMIT 1`,
        ),
      );
      if (row === null) return { status: PendingReviewStatus.NONE, event: null, entry: null };
      const entry = this.loadEntryById(row.entry_id);
      if (entry === null) throw new Error("missing review entry");
      return { status: PendingReviewStatus.PENDING, event: eventFromRow(row), entry };
    } catch {
      return { status: PendingReviewStatus.STORAGE_ERROR, event: null, entry: null };
    }
  }

  dailyReview(now = new Date()): ReviewPromptResult {
    const reviewDate = localDate(now, this.timeZone);
    try {
      return this.storage.transactionSync(() => {
        const active = oneOrNull(
          this.sql.exec<{ present: number }>(
            "SELECT 1 AS present FROM test_sessions WHERE status = 'active' LIMIT 1",
          ),
        );
        if (active !== null) {
          return { status: ReviewPromptStatus.TEST_ACTIVE, event: null, entry: null };
        }
        const current = oneOrNull(
          this.sql.exec<ReviewRow>("SELECT * FROM review_events WHERE review_date = ?", reviewDate),
        );
        if (current !== null) {
          const entry = this.loadEntryById(current.entry_id);
          if (entry === null) throw new Error("missing review entry");
          return {
            status:
              current.status === "pending"
                ? ReviewPromptStatus.PENDING
                : ReviewPromptStatus.ALREADY_COMPLETED,
            event: eventFromRow(current),
            entry,
          };
        }
        all(
          this.sql.exec(
            "UPDATE review_events SET status = 'missed' WHERE status = 'pending' AND review_date < ?",
            reviewDate,
          ),
        );
        const entryRow = oneOrNull(
          this.sql.exec<EntryRow>(
            `SELECT * FROM vocabulary_entries
             ORDER BY
               CASE WHEN last_reviewed IS NULL THEN 0 ELSE 1 END,
               COALESCE(last_reviewed, date_added),
               date_added,
               id
             LIMIT 1`,
          ),
        );
        if (entryRow === null) {
          return { status: ReviewPromptStatus.EMPTY, event: null, entry: null };
        }
        all(
          this.sql.exec(
            `INSERT INTO review_events (entry_id, review_date, status, prompted_at)
             VALUES (?, ?, 'pending', ?)`,
            entryRow.id,
            reviewDate,
            isoTimestamp(now),
          ),
        );
        const event = oneOrNull(
          this.sql.exec<ReviewRow>("SELECT * FROM review_events WHERE id = last_insert_rowid()"),
        );
        if (event === null) throw new Error("missing inserted review event");
        return {
          status: ReviewPromptStatus.PENDING,
          event: eventFromRow(event),
          entry: this.entryFromRow(entryRow),
        };
      });
    } catch {
      return { status: ReviewPromptStatus.STORAGE_ERROR, event: null, entry: null };
    }
  }

  completeReview(
    expectedEventId: number,
    answerText: string,
    evaluation: Evaluation | null,
    now = new Date(),
    persistResult?: (result: ReviewCompletionResult) => void,
  ): ReviewCompletionResult {
    if (trimPythonWhitespace(answerText).length === 0 || !isEvaluation(evaluation)) {
      return {
        status: ReviewCompletionStatus.INVALID,
        entry: null,
        answerText: null,
        grade: null,
        feedback: null,
        eventId: null,
      };
    }
    try {
      return this.storage.transactionSync(() => {
        const event = oneOrNull(
          this.sql.exec<ReviewRow>(
            "SELECT * FROM review_events WHERE id = ? AND status = 'pending'",
            expectedEventId,
          ),
        );
        if (event === null) {
          const result = {
            status: ReviewCompletionStatus.NO_PENDING,
            entry: null,
            answerText: null,
            grade: null,
            feedback: null,
            eventId: null,
          };
          persistResult?.(result);
          return result;
        }
        const updated = this.sql.exec(
          `UPDATE review_events
           SET status = 'answered', answered_at = ?, answer_text = ?, grade = ?, evaluation_feedback = ?
           WHERE id = ? AND status = 'pending'`,
          isoTimestamp(now),
          answerText,
          evaluation.grade,
          evaluation.feedback,
          expectedEventId,
        );
        all(updated);
        if (updated.rowsWritten !== 1) throw new ReviewCasLost();
        all(
          this.sql.exec(
            "UPDATE vocabulary_entries SET last_reviewed = ?, review_status = 'reviewed' WHERE id = ?",
            isoTimestamp(now),
            event.entry_id,
          ),
        );
        const entry = this.loadEntryById(event.entry_id);
        if (entry === null) throw new Error("missing reviewed entry");
        const result = {
          status: ReviewCompletionStatus.COMPLETED,
          entry,
          answerText,
          grade: evaluation.grade,
          feedback: evaluation.feedback,
          eventId: expectedEventId,
        };
        persistResult?.(result);
        return result;
      });
    } catch (error) {
      return {
        status:
          error instanceof ReviewCasLost
            ? ReviewCompletionStatus.NO_PENDING
            : ReviewCompletionStatus.STORAGE_ERROR,
        entry: null,
        answerText: null,
        grade: null,
        feedback: null,
        eventId: null,
      };
    }
  }

  currentTest(): TestSnapshotResult {
    try {
      const row = oneOrNull(
        this.sql.exec<SessionRow>("SELECT * FROM test_sessions WHERE status = 'active' LIMIT 1"),
      );
      return row === null
        ? { status: TestSnapshotStatus.NONE, snapshot: null }
        : { status: TestSnapshotStatus.ACTIVE, snapshot: this.testSnapshot(row) };
    } catch {
      return { status: TestSnapshotStatus.STORAGE_ERROR, snapshot: null };
    }
  }

  startTest(now = new Date()): TestStartResult {
    try {
      return this.storage.transactionSync(() => {
        const active = oneOrNull(
          this.sql.exec<SessionRow>("SELECT * FROM test_sessions WHERE status = 'active' LIMIT 1"),
        );
        if (active !== null) {
          return {
            status: TestStartStatus.RESUMED,
            snapshot: this.testSnapshot(active),
            availableCount: null,
            requiredCount: REQUIRED_TEST_QUESTIONS,
          };
        }
        const pending = oneOrNull(
          this.sql.exec<{ present: number }>(
            "SELECT 1 AS present FROM review_events WHERE status = 'pending' LIMIT 1",
          ),
        );
        if (pending !== null) {
          return {
            status: TestStartStatus.DAILY_REVIEW_PENDING,
            snapshot: null,
            availableCount: null,
            requiredCount: REQUIRED_TEST_QUESTIONS,
          };
        }
        const entries = all(
          this.sql.exec<{ id: number }>(
            `SELECT entry.id
             FROM vocabulary_entries AS entry
             LEFT JOIN (
               SELECT question.entry_id, MAX(session.started_at) AS last_tested_at
               FROM test_questions AS question
               JOIN test_sessions AS session ON session.id = question.session_id
               GROUP BY question.entry_id
             ) AS test_history ON test_history.entry_id = entry.id
             WHERE EXISTS (
               SELECT 1 FROM vocabulary_senses AS sense WHERE sense.entry_id = entry.id
             )
             ORDER BY
               CASE WHEN test_history.last_tested_at IS NULL THEN 0 ELSE 1 END,
               test_history.last_tested_at,
               CASE WHEN entry.last_reviewed IS NULL THEN 0 ELSE 1 END,
               COALESCE(entry.last_reviewed, entry.date_added),
               entry.date_added,
               entry.id
             LIMIT ?`,
            REQUIRED_TEST_QUESTIONS,
          ),
        );
        if (entries.length < REQUIRED_TEST_QUESTIONS) {
          return {
            status: TestStartStatus.INSUFFICIENT_LIBRARY,
            snapshot: null,
            availableCount: entries.length,
            requiredCount: REQUIRED_TEST_QUESTIONS,
          };
        }
        all(
          this.sql.exec(
            "INSERT INTO test_sessions (status, started_at) VALUES ('active', ?)",
            isoTimestamp(now),
          ),
        );
        const sessionId = oneOrNull(
          this.sql.exec<{ id: number }>("SELECT last_insert_rowid() AS id"),
        )?.id;
        if (sessionId === undefined) throw new Error("missing test session id");
        entries.forEach((entry, index) => {
          all(
            this.sql.exec(
              "INSERT INTO test_questions (session_id, entry_id, position) VALUES (?, ?, ?)",
              sessionId,
              entry.id,
              index + 1,
            ),
          );
        });
        const session = oneOrNull(
          this.sql.exec<SessionRow>("SELECT * FROM test_sessions WHERE id = ?", sessionId),
        );
        if (session === null) throw new Error("missing test session");
        return {
          status: TestStartStatus.STARTED,
          snapshot: this.testSnapshot(session),
          availableCount: null,
          requiredCount: REQUIRED_TEST_QUESTIONS,
        };
      });
    } catch {
      return {
        status: TestStartStatus.STORAGE_ERROR,
        snapshot: null,
        availableCount: null,
        requiredCount: REQUIRED_TEST_QUESTIONS,
      };
    }
  }

  completeTest(
    expectedQuestionId: number,
    answerText: string,
    evaluation: Evaluation | null,
    now = new Date(),
    persistResult?: (result: TestCompletionResult) => void,
  ): TestCompletionResult {
    if (trimPythonWhitespace(answerText).length === 0 || !isEvaluation(evaluation)) {
      return { status: TestCompletionStatus.INVALID, snapshot: null, answeredQuestion: null };
    }
    try {
      return this.storage.transactionSync(() => {
        const session = oneOrNull(
          this.sql.exec<SessionRow>("SELECT * FROM test_sessions WHERE status = 'active' LIMIT 1"),
        );
        if (session === null) {
          const result = { status: TestCompletionStatus.NO_ACTIVE, snapshot: null, answeredQuestion: null };
          persistResult?.(result);
          return result;
        }
        const current = oneOrNull(
          this.sql.exec<QuestionRow>(
            `SELECT * FROM test_questions
             WHERE session_id = ? AND answer_text IS NULL
             ORDER BY position LIMIT 1`,
            session.id,
          ),
        );
        if (current === null || current.id !== expectedQuestionId) {
          const result = {
            status: TestCompletionStatus.STALE,
            snapshot: this.testSnapshot(session),
            answeredQuestion: null,
          };
          persistResult?.(result);
          return result;
        }
        const answeredAt = isoTimestamp(now);
        const updated = this.sql.exec(
          `UPDATE test_questions
           SET answer_text = ?, grade = ?, evaluation_feedback = ?, answered_at = ?
           WHERE id = ? AND session_id = ? AND answer_text IS NULL`,
          answerText,
          evaluation.grade,
          evaluation.feedback,
          answeredAt,
          expectedQuestionId,
          session.id,
        );
        all(updated);
        if (updated.rowsWritten !== 1) throw new TestCasLost();
        let status: TestCompletionResult["status"] = TestCompletionStatus.ADVANCED;
        if (current.position === REQUIRED_TEST_QUESTIONS) {
          const completed = this.sql.exec(
            `UPDATE test_sessions SET status = 'completed', completed_at = ?
             WHERE id = ? AND status = 'active'`,
            answeredAt,
            session.id,
          );
          all(completed);
          if (completed.rowsWritten !== 1) throw new TestCasLost();
          status = TestCompletionStatus.COMPLETED;
        }
        const latest = oneOrNull(
          this.sql.exec<SessionRow>("SELECT * FROM test_sessions WHERE id = ?", session.id),
        );
        if (latest === null) throw new Error("missing updated test session");
        const snapshot = this.testSnapshot(latest);
        const answeredQuestion = snapshot.questions.find((question) => question.id === expectedQuestionId);
        if (answeredQuestion === undefined) throw new Error("missing answered question");
        const result = { status, snapshot, answeredQuestion };
        persistResult?.(result);
        return result;
      });
    } catch (error) {
      return {
        status:
          error instanceof TestCasLost
            ? TestCompletionStatus.STALE
            : TestCompletionStatus.STORAGE_ERROR,
        snapshot: null,
        answeredQuestion: null,
      };
    }
  }

  exportSnapshot(): SnapshotV1 {
    return {
      formatVersion: 1,
      entries: all(this.sql.exec<EntryRow>("SELECT * FROM vocabulary_entries ORDER BY id")).map(
        (row) => ({
          id: row.id,
          displayText: row.display_text,
          normalizedText: row.normalized_text,
          dateAdded: row.date_added,
          lastReviewed: row.last_reviewed,
          reviewStatus: row.review_status,
        }),
      ),
      senses: all(this.sql.exec<SenseRow>("SELECT * FROM vocabulary_senses ORDER BY id")).map(
        (row) => ({
          id: row.id,
          entryId: row.entry_id,
          definition: row.definition,
          partOfSpeech: row.part_of_speech,
          exampleSentence: row.example_sentence,
          sourceContext: row.source_context,
          dateAdded: row.date_added,
        }),
      ),
      reviewEvents: all(this.sql.exec<ReviewRow>("SELECT * FROM review_events ORDER BY id")).map(
        (row) => ({
          id: row.id,
          entryId: row.entry_id,
          reviewDate: row.review_date,
          status: row.status,
          promptedAt: row.prompted_at,
          answeredAt: row.answered_at,
          answerText: row.answer_text,
          grade: row.grade,
          evaluationFeedback: row.evaluation_feedback,
        }),
      ),
      testSessions: all(this.sql.exec<SessionRow>("SELECT * FROM test_sessions ORDER BY id")).map(
        (row) => ({
          id: row.id,
          status: row.status,
          startedAt: row.started_at,
          completedAt: row.completed_at,
        }),
      ),
      testQuestions: all(this.sql.exec<QuestionRow>("SELECT * FROM test_questions ORDER BY id")).map(
        (row) => ({
          id: row.id,
          sessionId: row.session_id,
          entryId: row.entry_id,
          position: row.position,
          answerText: row.answer_text,
          grade: row.grade,
          evaluationFeedback: row.evaluation_feedback,
          answeredAt: row.answered_at,
        }),
      ),
    };
  }

  importSnapshot(snapshot: SnapshotV1): void {
    this.storage.transactionSync(() => {
      for (const table of [
        "vocabulary_entries",
        "vocabulary_senses",
        "review_events",
        "test_sessions",
        "test_questions",
      ]) {
        const count = this.sql.exec<{ count: number }>(`SELECT COUNT(*) AS count FROM ${table}`).one().count;
        if (count !== 0) throw new Error("snapshot import requires empty storage");
      }
      for (const row of snapshot.entries) {
        all(this.sql.exec(
          `INSERT INTO vocabulary_entries
            (id, display_text, normalized_text, date_added, last_reviewed, review_status)
           VALUES (?, ?, ?, ?, ?, ?)`,
          row.id, row.displayText, row.normalizedText, row.dateAdded, row.lastReviewed, row.reviewStatus,
        ));
      }
      for (const row of snapshot.senses) {
        all(this.sql.exec(
          `INSERT INTO vocabulary_senses
            (id, entry_id, definition, part_of_speech, example_sentence, source_context, date_added)
           VALUES (?, ?, ?, ?, ?, ?, ?)`,
          row.id, row.entryId, row.definition, row.partOfSpeech, row.exampleSentence,
          row.sourceContext, row.dateAdded,
        ));
      }
      for (const row of snapshot.reviewEvents) {
        all(this.sql.exec(
          `INSERT INTO review_events
            (id, entry_id, review_date, status, prompted_at, answered_at, answer_text, grade, evaluation_feedback)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          row.id, row.entryId, row.reviewDate, row.status, row.promptedAt, row.answeredAt,
          row.answerText, row.grade, row.evaluationFeedback,
        ));
      }
      for (const row of snapshot.testSessions) {
        all(this.sql.exec(
          "INSERT INTO test_sessions (id, status, started_at, completed_at) VALUES (?, ?, ?, ?)",
          row.id, row.status, row.startedAt, row.completedAt,
        ));
      }
      for (const row of snapshot.testQuestions) {
        all(this.sql.exec(
          `INSERT INTO test_questions
            (id, session_id, entry_id, position, answer_text, grade, evaluation_feedback, answered_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
          row.id, row.sessionId, row.entryId, row.position, row.answerText, row.grade,
          row.evaluationFeedback, row.answeredAt,
        ));
      }
      const exported = this.exportSnapshot();
      const pairs: readonly [readonly { id: number }[], readonly { id: number }[]][] = [
        [snapshot.entries, exported.entries],
        [snapshot.senses, exported.senses],
        [snapshot.reviewEvents, exported.reviewEvents],
        [snapshot.testSessions, exported.testSessions],
        [snapshot.testQuestions, exported.testQuestions],
      ];
      for (const [expected, actual] of pairs) {
        if (
          expected.length !== actual.length ||
          (expected.at(-1)?.id ?? null) !== (actual.at(-1)?.id ?? null)
        ) throw new Error("snapshot import count or max-id mismatch");
      }
    });
  }

  private testSnapshot(sessionRow: SessionRow): TestSessionSnapshot {
    const rows = all(
      this.sql.exec<QuestionRow>(
        "SELECT * FROM test_questions WHERE session_id = ? ORDER BY position",
        sessionRow.id,
      ),
    );
    const questions: TestQuestion[] = rows.map((row) => {
      const entry = this.loadEntryById(row.entry_id);
      if (entry === null) throw new Error("missing test entry");
      return {
        id: row.id,
        sessionId: row.session_id,
        position: row.position,
        entry,
        answerText: row.answer_text,
        grade: row.grade,
        feedback: row.evaluation_feedback,
        answeredAt: row.answered_at,
      };
    });
    const summary = { correct: 0, partial: 0, incorrect: 0 };
    for (const question of questions) {
      if (question.grade !== null) summary[question.grade] += 1;
    }
    return {
      session: sessionFromRow(sessionRow),
      questions,
      currentQuestion: questions.find((question) => question.answerText === null) ?? null,
      summary,
    };
  }
}
