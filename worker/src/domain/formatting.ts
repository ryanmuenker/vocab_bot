import {
  CaptureStatus,
  ReviewCompletionStatus,
  ReviewPromptStatus,
  TestCompletionStatus,
  TestStartStatus,
} from "./models";
import type {
  CaptureResult,
  ReviewCompletionResult,
  ReviewPromptResult,
  TestCompletionResult,
  TestSessionSnapshot,
  TestStartResult,
  VocabularyEntry,
  VocabularySense,
} from "./models";
import { trimPythonWhitespace } from "./normalization";

type OptionalFields<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;
type CaptureResultInput = OptionalFields<CaptureResult, "entry" | "sense">;
type ReviewPromptResultInput = OptionalFields<ReviewPromptResult, "event" | "entry">;
type ReviewCompletionResultInput = OptionalFields<
  ReviewCompletionResult,
  "entry" | "answerText" | "grade" | "feedback" | "eventId"
>;
type TestStartResultInput = OptionalFields<
  TestStartResult,
  "snapshot" | "availableCount" | "requiredCount"
>;
type TestCompletionResultInput = OptionalFields<
  TestCompletionResult,
  "snapshot" | "answeredQuestion"
>;

function formatCaptureCard(
  entry: VocabularyEntry,
  sense: VocabularySense,
  footer: string,
): string {
  const firstCodePoint = entry.displayText.codePointAt(0);
  const firstCharacter =
    firstCodePoint === undefined ? "" : String.fromCodePoint(firstCodePoint);
  const displayText =
    firstCharacter.toUpperCase() + entry.displayText.slice(firstCharacter.length);
  return (
    `${displayText} (${sense.partOfSpeech})\n\n` +
    `Definition:\n${sense.definition}\n\n` +
    `Example:\n${sense.exampleSentence}\n\n` +
    footer
  );
}

export function formatEntry(entry: VocabularyEntry, footer: string): string {
  if (entry.senses.length === 1) {
    const sense = entry.senses[0]!;
    return (
      `${entry.displayText} (${sense.partOfSpeech})\n\n` +
      `Definition:\n${sense.definition}\n\n` +
      `Example:\n${sense.exampleSentence}\n\n` +
      footer
    );
  }
  let senses = "";
  for (let index = 0; index < entry.senses.length; index += 1) {
    const sense = entry.senses[index]!;
    if (index > 0) {
      senses += "\n\n";
    }
    senses +=
      `${index + 1}. ${sense.partOfSpeech}\n` +
      `Definition:\n${sense.definition}\n` +
      `Example:\n${sense.exampleSentence}`;
  }
  return `${entry.displayText}\n\n${senses}\n\n${footer}`;
}

export function formatHint(entry: VocabularyEntry): string {
  return `Hint: ${entry.senses[0]!.exampleSentence}`;
}

export function formatCapture(result: CaptureResultInput): string {
  if (result.status === CaptureStatus.INVALID) {
    return "Send a word or expression, optionally followed by context on the next line.";
  }
  if (result.status === CaptureStatus.CONFLICT) {
    return "That entry changed while I was saving it. Please try again.";
  }
  if (result.status === CaptureStatus.STORAGE_ERROR) {
    return "I couldn't save that entry. Please try again.";
  }
  if (result.entry == null || result.sense == null) {
    return "I couldn't save that entry. Please try again.";
  }
  if (result.status === CaptureStatus.ALREADY_EXISTS) {
    return formatCaptureCard(result.entry, result.sense, "Already saved with this meaning.");
  }
  if (result.status === CaptureStatus.NEW_SENSE_SAVED) {
    return formatCaptureCard(result.entry, result.sense, "✓ New meaning saved.");
  }
  return formatCaptureCard(result.entry, result.sense, "✓ Saved.");
}

export function formatDailyReview(result: ReviewPromptResultInput): string {
  if (
    result.status === ReviewPromptStatus.ALREADY_COMPLETED ||
    result.status === ReviewPromptStatus.TEST_ACTIVE
  ) {
    return "";
  }
  if (result.status === ReviewPromptStatus.EMPTY) {
    return "Save a word first, then I'll have something to review.";
  }
  if (result.status === ReviewPromptStatus.STORAGE_ERROR || result.entry == null) {
    throw new Error("Could not prepare the daily vocabulary review");
  }
  return `What does '${result.entry.displayText}' mean?`;
}

function formatCanonicalReveal(entry: VocabularyEntry): string {
  if (entry.senses.length === 1) {
    const sense = entry.senses[0]!;
    return `Definition:\n${sense.definition}\n\nExample:\n${sense.exampleSentence}`;
  }
  let reveal = "";
  for (let index = 0; index < entry.senses.length; index += 1) {
    const sense = entry.senses[index]!;
    if (index > 0) {
      reveal += "\n\n";
    }
    reveal +=
      `${index + 1}. ${sense.partOfSpeech} — ${sense.definition}\n` +
      `   Example: ${sense.exampleSentence}`;
  }
  return reveal;
}

function formatTestPrompt(snapshot: TestSessionSnapshot): string | null {
  const question = snapshot.currentQuestion;
  if (question === null) {
    return null;
  }
  return `Question ${question.position} of 5\nWhat does '${question.entry.displayText}' mean?`;
}

export function formatTestStart(result: TestStartResultInput): string {
  if (result.status === TestStartStatus.INSUFFICIENT_LIBRARY) {
    const available = result.availableCount || 0;
    const required = result.requiredCount ?? 5;
    const needed = Math.max(required - available, 0);
    const entryWord = available === 1 ? "entry" : "entries";
    return (
      `You have ${available} saved ${entryWord}. ` +
      `Save ${needed} more to start a ${required}-word test.`
    );
  }
  if (result.status === TestStartStatus.DAILY_REVIEW_PENDING) {
    return "Finish your daily review before starting a test.";
  }
  if (result.status === TestStartStatus.STORAGE_ERROR || result.snapshot == null) {
    return "I couldn't start the test. Please try again.";
  }
  const prompt = formatTestPrompt(result.snapshot);
  return prompt ?? "I couldn't start the test. Please try again.";
}

export function formatTestCompletion(result: TestCompletionResultInput): string {
  if (result.status === TestCompletionStatus.INVALID) {
    return "Send an answer, or type 'show answer'.";
  }
  if (result.status === TestCompletionStatus.NO_ACTIVE) {
    return "There isn't an active test.";
  }
  if (result.status === TestCompletionStatus.STALE) {
    if (result.snapshot == null) {
      return "That answer was already recorded.";
    }
    const prompt = formatTestPrompt(result.snapshot);
    return prompt === null
      ? "There isn't an active test."
      : `That answer was already recorded.\n\n${prompt}`;
  }

  const answered = result.answeredQuestion;
  if (
    result.status === TestCompletionStatus.STORAGE_ERROR ||
    result.snapshot == null ||
    answered == null ||
    answered.grade == null ||
    answered.feedback == null ||
    trimPythonWhitespace(answered.feedback).length === 0
  ) {
    return "I couldn't evaluate that answer. Please try again.";
  }

  const grade = answered.grade[0]!.toUpperCase() + answered.grade.slice(1);
  const text =
    `Grade: ${grade}\nFeedback: ${answered.feedback}\n\n` +
    formatCanonicalReveal(answered.entry);
  if (result.status === TestCompletionStatus.ADVANCED) {
    const prompt = formatTestPrompt(result.snapshot);
    return prompt === null
      ? "I couldn't evaluate that answer. Please try again."
      : `${text}\n\n${prompt}`;
  }
  if (result.status === TestCompletionStatus.COMPLETED) {
    const summary = result.snapshot.summary;
    return (
      `${text}\n\nTest complete.\n` +
      `Results: ${summary.correct} correct, ${summary.partial} partial, ` +
      `${summary.incorrect} incorrect.`
    );
  }
  return "I couldn't evaluate that answer. Please try again.";
}

export function formatReviewCompletion(result: ReviewCompletionResultInput): string {
  if (result.status === ReviewCompletionStatus.INVALID) {
    return "Send an answer, or type 'show answer'.";
  }
  if (result.status === ReviewCompletionStatus.NO_PENDING) {
    return "There isn't a review waiting.";
  }
  if (
    result.status === ReviewCompletionStatus.STORAGE_ERROR ||
    result.entry == null ||
    result.grade == null ||
    result.feedback == null ||
    trimPythonWhitespace(result.feedback).length === 0
  ) {
    return "I couldn't evaluate that answer. Please try again.";
  }
  const grade = result.grade[0]!.toUpperCase() + result.grade.slice(1);
  const evaluation = `Grade: ${grade}\nFeedback: ${result.feedback}`;
  return `${evaluation}\n\n${formatCanonicalReveal(result.entry)}`;
}
