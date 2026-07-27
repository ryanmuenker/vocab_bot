import { CaptureStatus, CardDirection, StudyMode } from "./models";
import type {
  CaptureResult,
  ReviewRating,
  StudyAnswerContext,
  StudyCardContext,
  StudyProgress,
  StudySnapshot,
  VocabularyEntry,
  VocabularySense,
} from "./models";

/** Directional tests always run a fixed five-card queue. */
const TEST_REQUIRED_CARDS = 5;

type OptionalFields<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;
type CaptureResultInput = OptionalFields<CaptureResult, "entry" | "sense">;

/**
 * Python `str.title()` over the single lowercase ASCII word that every
 * rating, grade, and direction value is.
 */
function titleCase(value: string): string {
  return value.slice(0, 1).toUpperCase() + value.slice(1);
}

function displayEntry(text: string): string {
  const firstCodePoint = text.codePointAt(0);
  if (firstCodePoint === undefined) {
    return text;
  }
  const firstCharacter = String.fromCodePoint(firstCodePoint);
  return firstCharacter.toUpperCase() + text.slice(firstCharacter.length);
}

function formatCaptureCard(
  entry: VocabularyEntry,
  sense: VocabularySense,
  footer: string,
): string {
  return (
    `${displayEntry(entry.displayText)} (${sense.partOfSpeech})\n\n` +
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

/** Every stored sense of an entry, revealed after an answer is graded. */
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

export function formatStudyPrompt(
  context: StudyCardContext,
  snapshot: StudySnapshot,
  options: { readonly dueBacklog: number },
): string {
  const retry = context.queueItem.retryOfQueueItemId !== null ? " · retry" : "";
  if (snapshot.mode === StudyMode.REVIEW) {
    const current = Math.min(snapshot.progress.completed + 1, snapshot.progress.total);
    const header =
      `Review ${current} of ${snapshot.progress.total} · ` +
      `${options.dueBacklog} due${retry}`;
    if (context.queueItem.card.direction === CardDirection.REVERSE) {
      if (context.sense === null) {
        throw new Error("reverse study prompts require a selected sense");
      }
      return (
        `${header}\nWhich saved word or expression matches this definition?\n\n` +
        context.sense.definition
      );
    }
    return `${header}\nWhat does '${context.entry.displayText}' mean?`;
  }

  // A tail retry always occupies the final question slot of a directional test.
  const position = retry === "" ? context.queueItem.position : TEST_REQUIRED_CARDS;
  const header = `Question ${position} of ${TEST_REQUIRED_CARDS}${retry}`;
  if (snapshot.mode === StudyMode.TEST_FORWARD) {
    return `${header}\nWhat does '${context.entry.displayText}' mean?`;
  }
  if (context.sense === null) {
    throw new Error("reverse test card has no sense definition");
  }
  return `${header}\nWhich saved word matches this definition?\n${context.sense.definition}`;
}

export function formatStudyEvaluationResult(context: StudyAnswerContext): string {
  const draft = context.draft;
  if (draft === null) {
    throw new Error("study evaluation formatting requires a persisted draft");
  }
  const evaluation = draft.evaluation;
  let reveal: string;
  if (context.queueItem.card.direction === CardDirection.REVERSE) {
    if (context.sense === null) {
      throw new Error("reverse evaluation requires a selected sense");
    }
    reveal =
      `Answer: ${context.entry.displayText}\n\n` +
      `Definition:\n${context.sense.definition}\n\n` +
      `Example:\n${context.sense.exampleSentence}`;
  } else {
    reveal = formatCanonicalReveal(context.entry);
  }
  return (
    `Grade: ${titleCase(evaluation.grade)}\n` +
    `Feedback: ${evaluation.feedback}\n\n` +
    reveal
  );
}

export function formatStudyEvaluation(
  context: StudyAnswerContext,
  choices: readonly ReviewRating[],
): string {
  const result = formatStudyEvaluationResult(context);
  if (choices.length === 0) {
    return result;
  }
  return `${result}\n\nChoose effort: ${choices.map(titleCase).join(" or ")}.`;
}

/**
 * Render a due instant in the learner's own clock.
 *
 * A bare UTC stamp is unreadable to anyone who is not on UTC: "08:03 UTC"
 * reads as morning to a learner whose watch says 16:03. The offset stays
 * attached so the value is still unambiguous. Mirrors `_format_due` in
 * `src/hermes_vocab/formatting.py`.
 */
function formatDue(effectiveDue: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "longOffset",
  }).formatToParts(effectiveDue);
  const field: Record<string, string> = {};
  for (const part of parts) field[part.type] = part.value;
  // `longOffset` renders UTC itself as a bare "GMT", every other zone as
  // "GMT+08:00"; Python writes the zero offset explicitly, so normalize.
  const offset = (field.timeZoneName ?? "GMT").replace("GMT", "") || "+00:00";
  return `${field.year}-${field.month}-${field.day} ${field.hour}:${field.minute} (UTC${offset})`;
}

export function formatStudySchedule(
  rating: ReviewRating,
  effectiveDue: Date,
  progress: StudyProgress,
  options: {
    readonly retryQueued: boolean;
    readonly timeZone: string;
    readonly nextPrompt?: string | null;
  },
): string {
  const due = formatDue(effectiveDue, options.timeZone);
  let text =
    `Rated: ${titleCase(rating)}\n` +
    `Next due: ${due}\n` +
    `Progress: ${progress.completed} of ${progress.total} complete.`;
  if (options.retryQueued) {
    text += "\nRetry added at the end.";
  }
  return options.nextPrompt ? `${text}\n\n${options.nextPrompt}` : text;
}

export function formatDirectionalTotals(
  direction: CardDirection,
  totals: {
    readonly correct: number;
    readonly partial: number;
    readonly incorrect: number;
  },
): string {
  return (
    `${titleCase(direction)} test complete.\n` +
    `Results: ${totals.correct} correct, ${totals.partial} partial, ` +
    `${totals.incorrect} incorrect.`
  );
}
