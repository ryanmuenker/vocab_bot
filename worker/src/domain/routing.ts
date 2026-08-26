import { CardDirection, ReviewRating } from "./models";
import type { EvaluationGrade } from "./models";
import { caseFold, trimPythonWhitespace } from "./normalization";

const HINT_REQUESTS: Record<string, true> = {
  hint: true,
  "give me a hint": true,
  "can i have a hint": true,
  "show me an example": true,
  "example sentence": true,
};
const PYTHON_WHITESPACE_RUN = /[\p{White_Space}\u001c-\u001f]+/gu;
const REVERSE_ANSWER_ALPHANUMERIC = /[\p{Letter}\p{Number}]/u;

export function isHintRequest(message: string): boolean {
  const collapsed = caseFold(trimPythonWhitespace(message).replace(PYTHON_WHITESPACE_RUN, " "));
  const normalized = trimPythonWhitespace(collapsed.replace(/[?.!]+$/u, ""));
  return Object.hasOwn(HINT_REQUESTS, normalized);
}

export function slashCommandName(message: string): string | null {
  if (!message.startsWith("/")) return null;
  const firstWhitespace = message.search(/[\p{White_Space}\u001c-\u001f]/u);
  const firstToken = (firstWhitespace === -1 ? message : message.slice(0, firstWhitespace)).slice(1);
  const token = firstToken.split("@", 1)[0] ?? "";
  return token.length > 0 && !token.includes("/") ? token : null;
}

export type StudyCommandName = "review" | "test" | "endstudy" | "pause" | "unpause";

export type StudyCommand =
  | { readonly kind: "review" }
  | { readonly kind: "test"; readonly direction: CardDirection }
  | { readonly kind: "endstudy" }
  | { readonly kind: "pause" }
  | { readonly kind: "unpause" }
  | { readonly kind: "usage"; readonly command: StudyCommandName };

const STUDY_COMMANDS: Record<string, StudyCommandName> = {
  review: "review",
  test: "test",
  endstudy: "endstudy",
  pause: "pause",
  unpause: "unpause",
};

/** Null means the message is not a study command and stays capturable. */
export function parseStudyCommand(message: string): StudyCommand | null {
  const name = slashCommandName(message);
  if (name === null || !Object.hasOwn(STUDY_COMMANDS, name)) return null;
  const command = STUDY_COMMANDS[name]!;
  const firstWhitespace = message.search(/[\p{White_Space}\u001c-\u001f]/u);
  const argumentsText =
    firstWhitespace === -1 ? "" : trimPythonWhitespace(message.slice(firstWhitespace));
  if (command === "test") {
    return argumentsText === CardDirection.FORWARD || argumentsText === CardDirection.REVERSE
      ? { kind: "test", direction: argumentsText }
      : { kind: "usage", command };
  }
  return argumentsText.length === 0 ? { kind: command } : { kind: "usage", command };
}

/** Effort ratings a grade may be settled with; incorrect settles itself. */
export function allowedRatings(grade: EvaluationGrade): readonly ReviewRating[] {
  if (grade === "partial") return [ReviewRating.AGAIN, ReviewRating.HARD];
  if (grade === "correct") return [ReviewRating.HARD, ReviewRating.GOOD, ReviewRating.EASY];
  return [];
}

export function parseRating(
  text: string,
  allowed: readonly ReviewRating[],
): ReviewRating | null {
  const token = caseFold(trimPythonWhitespace(text).replace(PYTHON_WHITESPACE_RUN, " "));
  return allowed.find((rating) => rating === token) ?? null;
}

/** Reverse cards are graded by canonical identity with punctuation and spacing ignored. */
export function normalizeReverseAnswer(text: string): string {
  const folded = caseFold(text.normalize("NFKC"));
  let normalized = "";
  for (const character of folded) {
    if (REVERSE_ANSWER_ALPHANUMERIC.test(character)) normalized += character;
  }
  return normalized;
}
