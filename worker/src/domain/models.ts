import type { ScheduleTransition } from "./scheduling";

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

export interface TestSummary {
  readonly correct: number;
  readonly partial: number;
  readonly incorrect: number;
}

export const ReviewRating = {
  AGAIN: "again",
  HARD: "hard",
  GOOD: "good",
  EASY: "easy",
} as const;
export type ReviewRating = (typeof ReviewRating)[keyof typeof ReviewRating];

export const CardScheduleState = {
  NEW: "new",
  REVIEW: "review",
  RELEARNING: "relearning",
} as const;
export type CardScheduleState = (typeof CardScheduleState)[keyof typeof CardScheduleState];

export const CardDirection = {
  FORWARD: "forward",
  REVERSE: "reverse",
} as const;
export type CardDirection = (typeof CardDirection)[keyof typeof CardDirection];

export const StudyMode = {
  REVIEW: "review",
  TEST_FORWARD: "test_forward",
  TEST_REVERSE: "test_reverse",
} as const;
export type StudyMode = (typeof StudyMode)[keyof typeof StudyMode];

export const StudySessionStatus = {
  ACTIVE: "active",
  INTERRUPTED: "interrupted",
  COMPLETED: "completed",
  EXITED: "exited",
} as const;
export type StudySessionStatus = (typeof StudySessionStatus)[keyof typeof StudySessionStatus];

export const StudyQueueStatus = {
  QUEUED: "queued",
  CURRENT: "current",
  COMPLETED: "completed",
  SKIPPED: "skipped",
} as const;
export type StudyQueueStatus = (typeof StudyQueueStatus)[keyof typeof StudyQueueStatus];

export const StudyPromptStatus = {
  PREPARED: "prepared",
  DELIVERED: "delivered",
  ANSWERED: "answered",
  COMPLETED: "completed",
  FAILED: "failed",
  CANCELLED: "cancelled",
} as const;
export type StudyPromptStatus = (typeof StudyPromptStatus)[keyof typeof StudyPromptStatus];

export interface StudyCardSnapshot {
  readonly id: number;
  readonly entryId: number;
  readonly senseId: number | null;
  readonly direction: CardDirection;
  readonly state: CardScheduleState;
  readonly stability: number | null;
  readonly difficulty: number | null;
  readonly due: Date;
  readonly effectiveDue: Date;
  readonly lastReview: Date | null;
  readonly repetitions: number;
  readonly lapses: number;
  readonly createdAt: Date;
}

export interface StudyQueueItemSnapshot {
  readonly id: number;
  readonly card: StudyCardSnapshot;
  readonly position: number;
  readonly status: StudyQueueStatus;
  readonly retryOfQueueItemId: number | null;
}

export interface StudyPromptSnapshot {
  readonly id: number;
  readonly sessionId: number;
  readonly queueItemId: number;
  readonly promptKey: string;
  readonly promptText: string;
  readonly status: StudyPromptStatus;
  readonly preparedAt: Date;
  readonly deliveredAt: Date | null;
  readonly answeredAt: Date | null;
}

export interface StudyProgress {
  readonly completed: number;
  readonly total: number;
}

export interface StudySnapshot {
  readonly sessionId: number;
  readonly mode: StudyMode;
  readonly status: StudySessionStatus;
  /** Local calendar day as `YYYY-MM-DD`. */
  readonly localDate: string;
  readonly queue: readonly StudyQueueItemSnapshot[];
  readonly currentPrompt: StudyPromptSnapshot | null;
  readonly progress: StudyProgress;
}

export interface StudyDraftSnapshot {
  readonly id: number;
  readonly submittedAnswer: string;
  readonly evaluation: Evaluation;
  readonly answeredAt: Date;
}

/** Everything a prompt renderer needs about the card currently under study. */
export interface StudyCardContext {
  readonly queueItem: StudyQueueItemSnapshot;
  readonly entry: VocabularyEntry;
  readonly sense: VocabularySense | null;
}

export interface StudyAnswerContext extends StudyCardContext {
  readonly prompt: StudyPromptSnapshot;
  readonly draft: StudyDraftSnapshot | null;
}

/** The data needed to render and persist the prompt for the current queue item. */
export interface StudyPromptPlan {
  readonly snapshot: StudySnapshot;
  readonly context: StudyCardContext;
  readonly promptKey: string;
}

export const StudyStartStatus = {
  STARTED: "started",
  RESUMED: "resumed",
  EMPTY: "empty",
  CONFLICT: "conflict",
  STORAGE_ERROR: "storage_error",
} as const;
export type StudyStartStatus = (typeof StudyStartStatus)[keyof typeof StudyStartStatus];

export interface StudyStartResult {
  readonly status: StudyStartStatus;
  readonly snapshot: StudySnapshot | null;
  /** Eligible cards found when a directional test is short of its five. */
  readonly availableCount: number | null;
}

export const FinalizeStatus = {
  COMPLETED: "completed",
  NO_ANSWER: "no_answer",
  STALE: "stale",
  STORAGE_ERROR: "storage_error",
} as const;
export type FinalizeStatus = (typeof FinalizeStatus)[keyof typeof FinalizeStatus];

export interface FinalizeResult {
  readonly status: FinalizeStatus;
  readonly transition: ScheduleTransition | null;
  readonly snapshot: StudySnapshot | null;
}

export const StudyMutationStatus = {
  COMPLETED: "completed",
  STALE: "stale",
  STORAGE_ERROR: "storage_error",
} as const;
export type StudyMutationStatus = (typeof StudyMutationStatus)[keyof typeof StudyMutationStatus];
