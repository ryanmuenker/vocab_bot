export type CaptureOperationValue = "new_entry" | "new_sense" | "existing_sense";

export class CaptureOperation {
  static readonly NEW_ENTRY = new CaptureOperation("new_entry");
  static readonly NEW_SENSE = new CaptureOperation("new_sense");
  static readonly EXISTING_SENSE = new CaptureOperation("existing_sense");

  private constructor(readonly value: CaptureOperationValue) {
    Object.freeze(this);
  }

  toString(): CaptureOperationValue {
    return this.value;
  }
}

export const CaptureStatus = {
  SAVED: "saved",
  NEW_SENSE_SAVED: "new_sense_saved",
  ALREADY_EXISTS: "already_exists",
  INVALID: "invalid",
  CONFLICT: "conflict",
  STORAGE_ERROR: "storage_error",
} as const;
export type CaptureStatus = (typeof CaptureStatus)[keyof typeof CaptureStatus];

export const EntryTextStatus = {
  VALID: "valid",
  EMPTY: "empty",
  TOO_LONG: "too_long",
} as const;
export type EntryTextStatus = (typeof EntryTextStatus)[keyof typeof EntryTextStatus];

export type NormalizedEntryText =
  | {
      readonly status: typeof EntryTextStatus.VALID;
      readonly displayText: string;
      readonly normalizedText: string;
    }
  | {
      readonly status: typeof EntryTextStatus.EMPTY | typeof EntryTextStatus.TOO_LONG;
    };

export interface CaptureRequest {
  readonly displayText: string;
  readonly context: string | null;
}

export interface SenseCard {
  readonly partOfSpeech: string;
  readonly definition: string;
  readonly exampleSentence: string;
}

export interface CaptureCommand {
  readonly displayText: string;
  readonly operation: CaptureOperation;
  readonly card: SenseCard | null;
  readonly sourceContext: string | null;
  readonly matchingSenseId: number | null;
}

export interface VocabularySense {
  readonly id: number;
  readonly entryId: number;
  readonly definition: string;
  readonly partOfSpeech: string;
  readonly exampleSentence: string;
  readonly sourceContext: string | null;
  readonly dateAdded: string;
}

export interface VocabularyEntry {
  readonly id: number;
  readonly displayText: string;
  readonly normalizedText: string;
  readonly dateAdded: string;
  readonly lastReviewed: string | null;
  readonly reviewStatus: "new" | "reviewed";
  readonly senses: readonly VocabularySense[];
}

export interface CaptureResult {
  readonly status: CaptureStatus;
  readonly entry: VocabularyEntry | null;
  readonly sense: VocabularySense | null;
}

export interface EntryCaptureResult {
  readonly status: CaptureStatus;
  readonly entry: VocabularyEntry | null;
}

export const EvaluationGrade = {
  CORRECT: "correct",
  PARTIAL: "partial",
  INCORRECT: "incorrect",
} as const;
export type EvaluationGrade = (typeof EvaluationGrade)[keyof typeof EvaluationGrade];

export interface Evaluation {
  readonly grade: EvaluationGrade;
  readonly feedback: string;
}

export const PendingReviewStatus = {
  PENDING: "pending",
  NONE: "none",
  STORAGE_ERROR: "storage_error",
} as const;
export type PendingReviewStatus =
  (typeof PendingReviewStatus)[keyof typeof PendingReviewStatus];

export const ReviewPromptStatus = {
  PENDING: "pending",
  ALREADY_COMPLETED: "already_completed",
  TEST_ACTIVE: "test_active",
  EMPTY: "empty",
  STORAGE_ERROR: "storage_error",
} as const;
export type ReviewPromptStatus = (typeof ReviewPromptStatus)[keyof typeof ReviewPromptStatus];

export const ReviewCompletionStatus = {
  COMPLETED: "completed",
  INVALID: "invalid",
  NO_PENDING: "no_pending",
  STORAGE_ERROR: "storage_error",
} as const;
export type ReviewCompletionStatus =
  (typeof ReviewCompletionStatus)[keyof typeof ReviewCompletionStatus];

export interface ReviewEvent {
  readonly id: number;
  readonly entryId: number;
  readonly reviewDate: string;
  readonly status: string;
  readonly promptedAt: string;
  readonly answeredAt: string | null;
  readonly answerText: string | null;
  readonly grade: EvaluationGrade | null;
  readonly feedback: string | null;
}

export interface PendingReviewResult {
  readonly status: PendingReviewStatus;
  readonly event: ReviewEvent | null;
  readonly entry: VocabularyEntry | null;
}

export interface ReviewPromptResult {
  readonly status: ReviewPromptStatus;
  readonly event: ReviewEvent | null;
  readonly entry: VocabularyEntry | null;
}

export interface ReviewCompletionResult {
  readonly status: ReviewCompletionStatus;
  readonly entry: VocabularyEntry | null;
  readonly answerText: string | null;
  readonly grade: EvaluationGrade | null;
  readonly feedback: string | null;
  readonly eventId: number | null;
}

export const TestSessionStatus = {
  ACTIVE: "active",
  COMPLETED: "completed",
} as const;
export type TestSessionStatus = (typeof TestSessionStatus)[keyof typeof TestSessionStatus];

export interface TestSession {
  readonly id: number;
  readonly status: TestSessionStatus;
  readonly startedAt: string;
  readonly completedAt: string | null;
}

export interface TestQuestion {
  readonly id: number;
  readonly sessionId: number;
  readonly position: number;
  readonly entry: VocabularyEntry;
  readonly answerText: string | null;
  readonly grade: EvaluationGrade | null;
  readonly feedback: string | null;
  readonly answeredAt: string | null;
}

export interface TestSummary {
  readonly correct: number;
  readonly partial: number;
  readonly incorrect: number;
}

export interface TestSessionSnapshot {
  readonly session: TestSession;
  readonly questions: readonly TestQuestion[];
  readonly currentQuestion: TestQuestion | null;
  readonly summary: TestSummary;
}

export const TestStartStatus = {
  STARTED: "started",
  RESUMED: "resumed",
  INSUFFICIENT_LIBRARY: "insufficient_library",
  DAILY_REVIEW_PENDING: "daily_review_pending",
  STORAGE_ERROR: "storage_error",
} as const;
export type TestStartStatus = (typeof TestStartStatus)[keyof typeof TestStartStatus];

export interface TestStartResult {
  readonly status: TestStartStatus;
  readonly snapshot: TestSessionSnapshot | null;
  readonly availableCount: number | null;
  readonly requiredCount: number;
}

export const TestSnapshotStatus = {
  ACTIVE: "active",
  NONE: "none",
  STORAGE_ERROR: "storage_error",
} as const;
export type TestSnapshotStatus = (typeof TestSnapshotStatus)[keyof typeof TestSnapshotStatus];

export interface TestSnapshotResult {
  readonly status: TestSnapshotStatus;
  readonly snapshot: TestSessionSnapshot | null;
}

export const TestCompletionStatus = {
  ADVANCED: "advanced",
  COMPLETED: "completed",
  INVALID: "invalid",
  STALE: "stale",
  NO_ACTIVE: "no_active",
  STORAGE_ERROR: "storage_error",
} as const;
export type TestCompletionStatus =
  (typeof TestCompletionStatus)[keyof typeof TestCompletionStatus];

export interface TestCompletionResult {
  readonly status: TestCompletionStatus;
  readonly snapshot: TestSessionSnapshot | null;
  readonly answeredQuestion: TestQuestion | null;
}
