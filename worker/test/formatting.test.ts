import { describe, expect, it } from "vitest";

import {
  CaptureStatus,
  EvaluationGrade,
  ReviewCompletionStatus,
  ReviewPromptStatus,
  TestCompletionStatus,
  TestSessionStatus,
  TestStartStatus,
  type TestQuestion,
  type TestSessionSnapshot,
  type VocabularyEntry,
  type VocabularySense,
} from "../src/domain/models";
import {
  formatCapture,
  formatDailyReview,
  formatEntry,
  formatHint,
  formatReviewCompletion,
  formatTestCompletion,
  formatTestStart,
} from "../src/domain/formatting";

const SENSE: VocabularySense = {
  id: 2,
  entryId: 1,
  definition: "Stubbornly refusing to change one's opinion.",
  partOfSpeech: "adjective",
  exampleSentence: "The committee remained obdurate despite new evidence.",
  sourceContext: "The committee stayed obdurate.",
  dateAdded: "2026-07-16T00:00:00Z",
};

const WORD: VocabularyEntry = {
  id: 1,
  displayText: "obdurate",
  normalizedText: "obdurate",
  dateAdded: "2026-07-16T00:00:00Z",
  lastReviewed: null,
  reviewStatus: "new",
  senses: [SENSE],
};

const SECOND_SENSE: VocabularySense = {
  ...SENSE,
  id: 3,
  definition: "Resistant to persuasion.",
  exampleSentence: "The witness remained obdurate.",
  sourceContext: null,
};

const MULTI_WORD: VocabularyEntry = {
  ...WORD,
  displayText: "pro forma",
  normalizedText: "pro forma",
  senses: [SENSE, SECOND_SENSE],
};

function question(
  position: number,
  overrides: Partial<TestQuestion> = {},
): TestQuestion {
  return {
    id: 10 + position,
    sessionId: 7,
    position,
    entry: WORD,
    answerText: null,
    grade: null,
    feedback: null,
    answeredAt: null,
    ...overrides,
  };
}

function snapshot(
  currentQuestion: TestQuestion | null,
  questions: readonly TestQuestion[],
  overrides: Partial<TestSessionSnapshot> = {},
): TestSessionSnapshot {
  return {
    session: {
      id: 7,
      status: currentQuestion === null ? TestSessionStatus.COMPLETED : TestSessionStatus.ACTIVE,
      startedAt: "2026-07-19T00:00:00Z",
      completedAt: currentQuestion === null ? "2026-07-19T00:05:00Z" : null,
    },
    questions,
    currentQuestion,
    summary: { correct: 0, partial: 0, incorrect: 0 },
    ...overrides,
  };
}

describe("entry, capture, and hint formatting", () => {
  it("matches the exact saved capture card", () => {
    expect(formatCapture({ status: CaptureStatus.SAVED, entry: WORD, sense: SENSE })).toBe(
      "Obdurate (adjective)\n\n" +
        "Definition:\n" +
        "Stubbornly refusing to change one's opinion.\n\n" +
        "Example:\n" +
        "The committee remained obdurate despite new evidence.\n\n" +
        "✓ Saved.",
    );
  });

  it("matches every capture status literal", () => {
    expect(formatCapture({ status: CaptureStatus.NEW_SENSE_SAVED, entry: WORD, sense: SENSE })).toBe(
      "Obdurate (adjective)\n\nDefinition:\nStubbornly refusing to change one's opinion.\n\n" +
        "Example:\nThe committee remained obdurate despite new evidence.\n\n✓ New meaning saved.",
    );
    expect(formatCapture({ status: CaptureStatus.ALREADY_EXISTS, entry: WORD, sense: SENSE })).toBe(
      "Obdurate (adjective)\n\nDefinition:\nStubbornly refusing to change one's opinion.\n\n" +
        "Example:\nThe committee remained obdurate despite new evidence.\n\nAlready saved with this meaning.",
    );
    expect(formatCapture({ status: CaptureStatus.INVALID })).toBe(
      "Send a word or expression, optionally followed by context on the next line.",
    );
    expect(formatCapture({ status: CaptureStatus.CONFLICT })).toBe(
      "That entry changed while I was saving it. Please try again.",
    );
    expect(formatCapture({ status: CaptureStatus.STORAGE_ERROR })).toBe(
      "I couldn't save that entry. Please try again.",
    );
    expect(formatCapture({ status: CaptureStatus.SAVED, entry: WORD })).toBe(
      "I couldn't save that entry. Please try again.",
    );
  });

  it("preserves exact single- and multi-sense entry whitespace and order", () => {
    expect(formatEntry(WORD, "Already saved.")).toBe(
      "obdurate (adjective)\n\n" +
        "Definition:\nStubbornly refusing to change one's opinion.\n\n" +
        "Example:\nThe committee remained obdurate despite new evidence.\n\nAlready saved.",
    );
    expect(formatEntry(MULTI_WORD, "✓ Saved.")).toBe(
      "pro forma\n\n" +
        "1. adjective\nDefinition:\nStubbornly refusing to change one's opinion.\n" +
        "Example:\nThe committee remained obdurate despite new evidence.\n\n" +
        "2. adjective\nDefinition:\nResistant to persuasion.\n" +
        "Example:\nThe witness remained obdurate.\n\n✓ Saved.",
    );
  });

  it("uses the first stored sense for hints", () => {
    expect(formatHint(MULTI_WORD)).toBe(
      "Hint: The committee remained obdurate despite new evidence.",
    );
  });
});

describe("daily review formatting", () => {
  it("matches all review prompt status strings", () => {
    expect(formatDailyReview({ status: ReviewPromptStatus.PENDING, entry: WORD })).toBe(
      "What does 'obdurate' mean?",
    );
    expect(formatDailyReview({ status: ReviewPromptStatus.ALREADY_COMPLETED })).toBe("");
    expect(formatDailyReview({ status: ReviewPromptStatus.TEST_ACTIVE })).toBe("");
    expect(formatDailyReview({ status: ReviewPromptStatus.EMPTY })).toBe(
      "Save a word first, then I'll have something to review.",
    );
    expect(() => formatDailyReview({ status: ReviewPromptStatus.STORAGE_ERROR })).toThrow(
      "Could not prepare the daily vocabulary review",
    );
  });

  it("formats grade and feedback before canonical one- and multi-sense reveals", () => {
    expect(
      formatReviewCompletion({
        status: ReviewCompletionStatus.COMPLETED,
        entry: WORD,
        answerText: "It means stubborn.",
        grade: EvaluationGrade.CORRECT,
        feedback: "Accurate paraphrase.",
        eventId: 7,
      }),
    ).toBe(
      "Grade: Correct\nFeedback: Accurate paraphrase.\n\n" +
        "Definition:\nStubbornly refusing to change one's opinion.\n\n" +
        "Example:\nThe committee remained obdurate despite new evidence.",
    );
    expect(
      formatReviewCompletion({
        status: ReviewCompletionStatus.COMPLETED,
        entry: MULTI_WORD,
        grade: EvaluationGrade.PARTIAL,
        feedback: "You identified part of the meaning.",
      }),
    ).toBe(
      "Grade: Partial\nFeedback: You identified part of the meaning.\n\n" +
        "1. adjective — Stubbornly refusing to change one's opinion.\n" +
        "   Example: The committee remained obdurate despite new evidence.\n\n" +
        "2. adjective — Resistant to persuasion.\n" +
        "   Example: The witness remained obdurate.",
    );
  });

  it("matches every review completion failure literal without revealing", () => {
    expect(formatReviewCompletion({ status: ReviewCompletionStatus.INVALID })).toBe(
      "Send an answer, or type 'show answer'.",
    );
    expect(formatReviewCompletion({ status: ReviewCompletionStatus.NO_PENDING })).toBe(
      "There isn't a review waiting.",
    );
    expect(formatReviewCompletion({ status: ReviewCompletionStatus.STORAGE_ERROR })).toBe(
      "I couldn't evaluate that answer. Please try again.",
    );
    expect(
      formatReviewCompletion({
        status: ReviewCompletionStatus.COMPLETED,
        entry: WORD,
        grade: EvaluationGrade.CORRECT,
        feedback: "\u001c",
      }),
    ).toBe("I couldn't evaluate that answer. Please try again.");
  });
});

describe("test formatting", () => {
  it("matches every test start literal", () => {
    const current = question(3);
    expect(
      formatTestStart({
        status: TestStartStatus.RESUMED,
        snapshot: snapshot(current, [current]),
      }),
    ).toBe("Question 3 of 5\nWhat does 'obdurate' mean?");
    expect(
      formatTestStart({ status: TestStartStatus.INSUFFICIENT_LIBRARY, availableCount: 3 }),
    ).toBe("You have 3 saved entries. Save 2 more to start a 5-word test.");
    expect(
      formatTestStart({ status: TestStartStatus.INSUFFICIENT_LIBRARY, availableCount: 1 }),
    ).toBe("You have 1 saved entry. Save 4 more to start a 5-word test.");
    expect(formatTestStart({ status: TestStartStatus.DAILY_REVIEW_PENDING })).toBe(
      "Finish your daily review before starting a test.",
    );
    expect(formatTestStart({ status: TestStartStatus.STORAGE_ERROR })).toBe(
      "I couldn't start the test. Please try again.",
    );
  });

  it("formats an advanced answer before the next exact prompt", () => {
    const answered = question(1, {
      answerText: "It means stubborn.",
      grade: EvaluationGrade.PARTIAL,
      feedback: "Right direction.",
      answeredAt: "2026-07-19T00:01:00Z",
    });
    const current = question(2);
    expect(
      formatTestCompletion({
        status: TestCompletionStatus.ADVANCED,
        snapshot: snapshot(current, [answered, current], {
          summary: { correct: 0, partial: 1, incorrect: 0 },
        }),
        answeredQuestion: answered,
      }),
    ).toBe(
      "Grade: Partial\nFeedback: Right direction.\n\n" +
        "Definition:\nStubbornly refusing to change one's opinion.\n\n" +
        "Example:\nThe committee remained obdurate despite new evidence.\n\n" +
        "Question 2 of 5\nWhat does 'obdurate' mean?",
    );
  });

  it("formats a completed answer and exact category totals", () => {
    const answered = question(5, {
      answerText: "attempt",
      grade: EvaluationGrade.INCORRECT,
      feedback: "That is not the stored meaning.",
      answeredAt: "2026-07-19T00:05:00Z",
    });
    expect(
      formatTestCompletion({
        status: TestCompletionStatus.COMPLETED,
        snapshot: snapshot(null, [answered], {
          summary: { correct: 2, partial: 2, incorrect: 1 },
        }),
        answeredQuestion: answered,
      }),
    ).toBe(
      "Grade: Incorrect\nFeedback: That is not the stored meaning.\n\n" +
        "Definition:\nStubbornly refusing to change one's opinion.\n\n" +
        "Example:\nThe committee remained obdurate despite new evidence.\n\n" +
        "Test complete.\nResults: 2 correct, 2 partial, 1 incorrect.",
    );
  });

  it("matches invalid, absent, stale, and evaluation failure literals", () => {
    expect(formatTestCompletion({ status: TestCompletionStatus.INVALID })).toBe(
      "Send an answer, or type 'show answer'.",
    );
    expect(formatTestCompletion({ status: TestCompletionStatus.NO_ACTIVE })).toBe(
      "There isn't an active test.",
    );
    expect(formatTestCompletion({ status: TestCompletionStatus.STALE })).toBe(
      "That answer was already recorded.",
    );
    const current = question(2);
    expect(
      formatTestCompletion({
        status: TestCompletionStatus.STALE,
        snapshot: snapshot(current, [current]),
      }),
    ).toBe(
      "That answer was already recorded.\n\nQuestion 2 of 5\nWhat does 'obdurate' mean?",
    );
    expect(formatTestCompletion({ status: TestCompletionStatus.STORAGE_ERROR })).toBe(
      "I couldn't evaluate that answer. Please try again.",
    );
  });
});
