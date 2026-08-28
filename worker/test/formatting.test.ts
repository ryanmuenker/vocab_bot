import { describe, expect, it } from "vitest";

import {
  CaptureStatus,
  CardDirection,
  CardScheduleState,
  EvaluationGrade,
  ReviewRating,
  StudyMode,
  StudyPromptStatus,
  StudyQueueStatus,
  StudySessionStatus,
  type StudyAnswerContext,
  type StudyCardSnapshot,
  type StudyPromptSnapshot,
  type StudyQueueItemSnapshot,
  type StudySnapshot,
  type VocabularyEntry,
  type VocabularySense,
} from "../src/domain/models";
import {
  formatCapture,
  formatDirectionalTotals,
  formatEntry,
  formatHint,
  formatWikimediaCaption,
  formatStudyEvaluation,
  formatStudyEvaluationResult,
  formatStudyPrompt,
  formatStudySchedule,
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

const MULTI_SENSE_WORD: VocabularyEntry = {
  ...WORD,
  senses: [SENSE, SECOND_SENSE],
};

const MULTI_WORD: VocabularyEntry = {
  ...MULTI_SENSE_WORD,
  displayText: "pro forma",
  normalizedText: "pro forma",
};

interface StudyContextOptions {
  readonly direction?: CardDirection;
  readonly entry?: VocabularyEntry;
  readonly sense?: VocabularySense | null;
  readonly grade?: EvaluationGrade | null;
  readonly retry?: boolean;
}

function studyContext(options: StudyContextOptions = {}): StudyAnswerContext {
  const direction = options.direction ?? CardDirection.FORWARD;
  const entry = options.entry ?? WORD;
  const sense = options.sense ?? null;
  const grade = options.grade ?? null;
  const retry = options.retry ?? false;

  const card: StudyCardSnapshot = {
    id: 101,
    entryId: entry.id,
    senseId: sense === null ? null : sense.id,
    direction,
    state: CardScheduleState.NEW,
    stability: null,
    difficulty: null,
    due: new Date("2026-07-20T12:00:00Z"),
    effectiveDue: new Date("2026-07-20T12:00:00Z"),
    lastReview: null,
    repetitions: 0,
    lapses: 0,
    createdAt: new Date("2026-07-01T00:00:00Z"),
  };
  const queueItem: StudyQueueItemSnapshot = {
    id: 201,
    card,
    position: retry ? 3 : 1,
    status: StudyQueueStatus.CURRENT,
    retryOfQueueItemId: retry ? 99 : null,
  };
  const prompt: StudyPromptSnapshot = {
    id: 301,
    sessionId: 401,
    queueItemId: queueItem.id,
    promptKey: "prompt-1",
    promptText: "Persisted.",
    status: grade === null ? StudyPromptStatus.DELIVERED : StudyPromptStatus.ANSWERED,
    preparedAt: new Date("2026-07-20T12:00:00Z"),
    deliveredAt: new Date("2026-07-20T12:00:00Z"),
    answeredAt: grade === null ? null : new Date("2026-07-20T12:01:00Z"),
  };
  return {
    prompt,
    queueItem,
    entry,
    sense,
    draft:
      grade === null
        ? null
        : {
            id: 501,
            submittedAnswer: "learner answer",
            evaluation: { grade, feedback: "Right direction." },
            answeredAt: new Date("2026-07-20T12:01:00Z"),
          },
  };
}

function studySnapshot(
  context: StudyAnswerContext,
  progress: { completed: number; total: number },
  mode: StudyMode = StudyMode.REVIEW,
): StudySnapshot {
  return {
    sessionId: context.prompt.sessionId,
    mode,
    status: StudySessionStatus.ACTIVE,
    localDate: "2026-07-20",
    queue: [context.queueItem],
    currentPrompt: context.prompt,
    progress,
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
        "Example:\nThe committee remained obdurate despite new evidence.\n\n" +
        "Already saved with this meaning.",
    );
    const conflict = formatCapture({ status: CaptureStatus.CONFLICT });
    expect(conflict).toBe("That entry changed while I was saving it. Please try again.");
    expect(conflict).not.toContain("Saved");
    expect(formatCapture({ status: CaptureStatus.INVALID })).toBe(
      "Send a word or expression, optionally followed by context on the next line.",
    );
    expect(formatCapture({ status: CaptureStatus.STORAGE_ERROR })).toBe(
      "I couldn't save that entry. Please try again.",
    );
    expect(formatCapture({ status: CaptureStatus.SAVED, entry: WORD })).toBe(
      "I couldn't save that entry. Please try again.",
    );
  });

  it("preserves the one-sense entry shape with the requested footer", () => {
    expect(formatEntry(WORD, "Already saved.")).toBe(
      "obdurate (adjective)\n\n" +
        "Definition:\n" +
        "Stubbornly refusing to change one's opinion.\n\n" +
        "Example:\n" +
        "The committee remained obdurate despite new evidence.\n\n" +
        "Already saved.",
    );
  });

  it("preserves all senses in database order", () => {
    expect(formatEntry(MULTI_WORD, "✓ Saved.")).toBe(
      "pro forma\n\n" +
        "1. adjective\n" +
        "Definition:\n" +
        "Stubbornly refusing to change one's opinion.\n" +
        "Example:\n" +
        "The committee remained obdurate despite new evidence.\n\n" +
        "2. adjective\n" +
        "Definition:\n" +
        "Resistant to persuasion.\n" +
        "Example:\n" +
        "The witness remained obdurate.\n\n" +
        "✓ Saved.",
    );
  });

  it("returns the complete stored example as a hint", () => {
    expect(formatHint(WORD)).toBe(
      "Hint: The committee remained obdurate despite new evidence.",
    );
  });

  it("uses the first stored sense for a multi-sense hint", () => {
    expect(formatHint(MULTI_SENSE_WORD)).toBe(
      "Hint: The committee remained obdurate despite new evidence.",
    );
  });
});

describe("study prompt formatting", () => {
  it("formats current, total, backlog, and tail retry exactly", () => {
    const current = studyContext();
    const retry = studyContext({ retry: true });

    expect(
      formatStudyPrompt(current, studySnapshot(current, { completed: 0, total: 3 }), {
        dueBacklog: 2,
      }),
    ).toBe("Review 1 of 3 · 2 due\nWhat does 'obdurate' mean?");
    expect(
      formatStudyPrompt(retry, studySnapshot(retry, { completed: 2, total: 3 }), {
        dueBacklog: 1,
      }),
    ).toBe("Review 3 of 3 · 1 due · retry\nWhat does 'obdurate' mean?");
  });

  it("clamps the current position to the session total", () => {
    const context = studyContext();

    expect(
      formatStudyPrompt(context, studySnapshot(context, { completed: 3, total: 3 }), {
        dueBacklog: 0,
      }),
    ).toBe("Review 3 of 3 · 0 due\nWhat does 'obdurate' mean?");
  });

  it("shows only the selected definition for a reverse multi-sense prompt", () => {
    const context = studyContext({
      direction: CardDirection.REVERSE,
      entry: MULTI_SENSE_WORD,
      sense: SECOND_SENSE,
    });

    const text = formatStudyPrompt(
      context,
      studySnapshot(context, { completed: 0, total: 5 }),
      { dueBacklog: 4 },
    );

    expect(text).toBe(
      "Review 1 of 5 · 4 due\n" +
        "Which saved word or expression matches this definition?\n\n" +
        "Resistant to persuasion.",
    );
    expect(text.toLowerCase()).not.toContain("obdurate");
    expect(text).not.toContain(SECOND_SENSE.exampleSentence);
    expect(text).not.toContain(SENSE.definition);
  });

  it("rejects a reverse prompt without a selected sense", () => {
    const context = studyContext({ direction: CardDirection.REVERSE });

    expect(() =>
      formatStudyPrompt(context, studySnapshot(context, { completed: 0, total: 1 }), {
        dueBacklog: 0,
      }),
    ).toThrow("reverse study prompts require a selected sense");
  });

  it("uses the directional test header in test modes", () => {
    const forward = studyContext();
    expect(
      formatStudyPrompt(
        forward,
        studySnapshot(forward, { completed: 0, total: 5 }, StudyMode.TEST_FORWARD),
        { dueBacklog: 4 },
      ),
    ).toBe("Question 1 of 5\nWhat does 'obdurate' mean?");

    const reverse = studyContext({
      direction: CardDirection.REVERSE,
      entry: MULTI_SENSE_WORD,
      sense: SECOND_SENSE,
    });
    expect(
      formatStudyPrompt(
        reverse,
        studySnapshot(reverse, { completed: 0, total: 5 }, StudyMode.TEST_REVERSE),
        { dueBacklog: 4 },
      ),
    ).toBe(
      "Question 1 of 5\nWhich saved word matches this definition?\nResistant to persuasion.",
    );
  });

  it("pins a test retry to the last question slot", () => {
    const retry = studyContext({ retry: true });

    expect(
      formatStudyPrompt(
        retry,
        studySnapshot(retry, { completed: 4, total: 5 }, StudyMode.TEST_FORWARD),
        { dueBacklog: 0 },
      ),
    ).toBe("Question 5 of 5 · retry\nWhat does 'obdurate' mean?");
  });
});

describe("study evaluation formatting", () => {
  it("reveals grade and feedback before the allowed choices", () => {
    const context = studyContext({ grade: EvaluationGrade.PARTIAL });

    const text = formatStudyEvaluation(context, [ReviewRating.AGAIN, ReviewRating.HARD]);

    expect(text).toBe(
      "Grade: Partial\n" +
        "Feedback: Right direction.\n\n" +
        "Definition:\n" +
        "Stubbornly refusing to change one's opinion.\n\n" +
        "Example:\n" +
        "The committee remained obdurate despite new evidence.\n\n" +
        "Choose effort: Again or Hard.",
    );
    expect(text.indexOf("Grade:")).toBeLessThan(text.indexOf("Definition:"));
    expect(text.indexOf("Definition:")).toBeLessThan(text.indexOf("Choose effort:"));
  });

  it("joins three allowed choices in order", () => {
    const context = studyContext({ grade: EvaluationGrade.CORRECT });

    expect(
      formatStudyEvaluation(context, [ReviewRating.HARD, ReviewRating.GOOD, ReviewRating.EASY]),
    ).toBe(
      "Grade: Correct\n" +
        "Feedback: Right direction.\n\n" +
        "Definition:\n" +
        "Stubbornly refusing to change one's opinion.\n\n" +
        "Example:\n" +
        "The committee remained obdurate despite new evidence.\n\n" +
        "Choose effort: Hard or Good or Easy.",
    );
  });

  it("omits the effort prompt entirely when there are no choices", () => {
    const context = studyContext({ grade: EvaluationGrade.INCORRECT });

    const text = formatStudyEvaluation(context, []);

    expect(text).toBe(formatStudyEvaluationResult(context));
    expect(text).not.toContain("Choose effort:");
  });

  it("reveals a finalized incorrect answer without an empty effort prompt", () => {
    const context = studyContext({ grade: EvaluationGrade.INCORRECT });

    const text = formatStudyEvaluationResult(context);

    expect(text).toBe(
      "Grade: Incorrect\n" +
        "Feedback: Right direction.\n\n" +
        "Definition:\n" +
        "Stubbornly refusing to change one's opinion.\n\n" +
        "Example:\n" +
        "The committee remained obdurate despite new evidence.",
    );
    expect(text).not.toContain("Choose effort:");
  });

  it("reveals every stored sense for a multi-sense forward card", () => {
    const context = studyContext({
      entry: MULTI_SENSE_WORD,
      grade: EvaluationGrade.PARTIAL,
    });

    expect(formatStudyEvaluationResult(context)).toBe(
      "Grade: Partial\n" +
        "Feedback: Right direction.\n\n" +
        "1. adjective — Stubbornly refusing to change one's opinion.\n" +
        "   Example: The committee remained obdurate despite new evidence.\n\n" +
        "2. adjective — Resistant to persuasion.\n" +
        "   Example: The witness remained obdurate.",
    );
  });

  it("reveals the answer and only the selected sense for a reverse card", () => {
    const context = studyContext({
      direction: CardDirection.REVERSE,
      entry: MULTI_SENSE_WORD,
      sense: SECOND_SENSE,
      grade: EvaluationGrade.CORRECT,
    });

    expect(formatStudyEvaluationResult(context)).toBe(
      "Grade: Correct\n" +
        "Feedback: Right direction.\n\n" +
        "Answer: obdurate\n\n" +
        "Definition:\n" +
        "Resistant to persuasion.\n\n" +
        "Example:\n" +
        "The witness remained obdurate.",
    );
  });

  it("rejects evaluation formatting without a persisted draft", () => {
    expect(() => formatStudyEvaluationResult(studyContext())).toThrow(
      "study evaluation formatting requires a persisted draft",
    );
  });

  it("rejects a reverse evaluation without a selected sense", () => {
    const context = studyContext({
      direction: CardDirection.REVERSE,
      grade: EvaluationGrade.CORRECT,
    });

    expect(() => formatStudyEvaluationResult(context)).toThrow(
      "reverse evaluation requires a selected sense",
    );
  });
});

describe("study schedule formatting", () => {
  it("formats progress, retry, and the next prompt", () => {
    const nextPrompt = "Review 2 of 3 · 1 due\nWhat does 'laconic' mean?";

    expect(
      formatStudySchedule(
        ReviewRating.AGAIN,
        new Date("2026-07-21T04:00:00Z"),
        { completed: 1, total: 3 },
        { retryQueued: true, timeZone: "Asia/Kuala_Lumpur", nextPrompt },
      ),
    ).toBe(
      "Rated: Again\n" +
        "Next due: 2026-07-21 12:00 (UTC+08:00)\n" +
        "Progress: 1 of 3 complete.\n" +
        "Retry added at the end.\n\n" +
        "Review 2 of 3 · 1 due\n" +
        "What does 'laconic' mean?",
    );
  });

  it("omits the retry line and the next prompt when neither applies", () => {
    expect(
      formatStudySchedule(
        ReviewRating.EASY,
        new Date("2026-08-03T09:07:00Z"),
        { completed: 3, total: 3 },
        { retryQueued: false, timeZone: "Asia/Kuala_Lumpur" },
      ),
    ).toBe(
      "Rated: Easy\nNext due: 2026-08-03 17:07 (UTC+08:00)\nProgress: 3 of 3 complete.",
    );
  });

  it("renders the due instant in the learner's zone, matching Python", () => {
    // A bare UTC stamp reads as the wrong time of day to anyone off UTC.
    const due = new Date("2026-07-28T08:03:00Z");
    for (const [timeZone, expected] of [
      ["UTC", "2026-07-28 08:03 (UTC+00:00)"],
      ["Asia/Kuala_Lumpur", "2026-07-28 16:03 (UTC+08:00)"],
      ["Asia/Kathmandu", "2026-07-28 13:48 (UTC+05:45)"],
      ["America/New_York", "2026-07-28 04:03 (UTC-04:00)"],
    ] as const) {
      expect(
        formatStudySchedule(ReviewRating.GOOD, due, { completed: 1, total: 2 }, {
          retryQueued: false,
          timeZone,
        }),
      ).toContain(`Next due: ${expected}`);
    }
  });
});

describe("directional totals formatting", () => {
  it("matches the exact forward and reverse totals", () => {
    expect(
      formatDirectionalTotals(CardDirection.FORWARD, {
        correct: 2,
        partial: 2,
        incorrect: 1,
      }),
    ).toBe("Forward test complete.\nResults: 2 correct, 2 partial, 1 incorrect.");
    expect(
      formatDirectionalTotals(CardDirection.REVERSE, {
        correct: 4,
        partial: 0,
        incorrect: 1,
      }),
    ).toBe("Reverse test complete.\nResults: 4 correct, 0 partial, 1 incorrect.");
  });
});

describe("Wikimedia caption formatting", () => {
  const ATTRIBUTION = {
    imageDescription: "Plain columns of the Doric architectural order.",
    creator: "Jane Smith",
    credit: "Own work",
    licenseName: "CC BY-SA 4.0",
    licenseUrl: "https://creativecommons.org/licenses/by-sa/4.0/",
    sourceUrl: "https://commons.wikimedia.org/wiki/File:Doric_columns.jpg",
  } as const;

  it("formats image description and complete plain-text attribution without repeating the definition", () => {
    expect(formatWikimediaCaption(ATTRIBUTION)).toBe(
      "Plain columns of the Doric architectural order.\n\n" +
        "Creator: Jane Smith\n" +
        "Credit: Own work\n" +
        "License: CC BY-SA 4.0 — https://creativecommons.org/licenses/by-sa/4.0/\n" +
        "Source: Wikimedia Commons — https://commons.wikimedia.org/wiki/File:Doric_columns.jpg",
    );
  });

  it("supports recognized public-domain attribution without inventing a license link", () => {
    expect(formatWikimediaCaption({
      ...ATTRIBUTION,
      creator: null,
      credit: null,
      licenseName: "Public domain",
      licenseUrl: null,
    })).toContain(
      "License: Public domain\n" +
        "Source: Wikimedia Commons — https://commons.wikimedia.org/wiki/File:Doric_columns.jpg",
    );
  });

  it("truncates only optional prose and preserves every mandatory boundary", () => {
    const caption = formatWikimediaCaption({
      ...ATTRIBUTION,
      imageDescription: "decorative column ".repeat(200),
    });

    expect(caption).not.toBeNull();
    expect(Array.from(caption!).length).toBeLessThanOrEqual(1024);
    expect(caption).toContain("\n\nCreator: Jane Smith\n");
    expect(caption).toContain("Credit: Own work\n");
    expect(caption).toContain(
      "License: CC BY-SA 4.0 — https://creativecommons.org/licenses/by-sa/4.0/\n",
    );
    expect(caption!.endsWith(
      "Source: Wikimedia Commons — https://commons.wikimedia.org/wiki/File:Doric_columns.jpg",
    )).toBe(true);
    expect(caption).toContain("decorative column decorative column");
    expect(caption).toContain("…\n\nCreator:");
  });

  it("rejects attribution whose mandatory content cannot fit the Telegram caption", () => {
    expect(formatWikimediaCaption({
      ...ATTRIBUTION,
      creator: "C".repeat(900),
      imageDescription: "",
    })).toBeNull();
  });
});
