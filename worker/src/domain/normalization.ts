import { CASE_FOLD_MAP } from "./casefold-map.generated";
import { CaptureOperation, EntryTextStatus } from "./models";
import type {
  CaptureCommand,
  CaptureRequest,
  NormalizedEntryText,
  SenseCard,
} from "./models";

export const MAX_ENTRY_TEXT_LENGTH = 500;
export const MAX_PART_OF_SPEECH_LENGTH = 50;
export const MAX_SENSE_TEXT_LENGTH = 500;
export const MAX_SOURCE_CONTEXT_LENGTH = 2_000;
export const MAX_SENSE_COUNT = 20;

const PYTHON_WHITESPACE_CLASS = "[\\p{White_Space}\\u001c-\\u001f]";
const PYTHON_WHITESPACE_EDGES = new RegExp(
  `^${PYTHON_WHITESPACE_CLASS}+|${PYTHON_WHITESPACE_CLASS}+$`,
  "gu",
);
const PYTHON_WHITESPACE_RUN = new RegExp(`${PYTHON_WHITESPACE_CLASS}+`, "gu");
const PYTHON_LINE_BOUNDARY = /\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/u;

export type SenseCardsValidation =
  | { readonly valid: true; readonly cards: readonly SenseCard[] }
  | { readonly valid: false; readonly reason: "count" | "card" | "duplicate" };

export interface PreparedCaptureCommand {
  readonly displayText: string;
  readonly normalizedText: string;
  readonly operation: CaptureOperation;
  readonly card: SenseCard | null;
  readonly sourceContext: string | null;
  readonly matchingSenseId: number | null;
}

export function trimPythonWhitespace(value: string): string {
  return value.replace(PYTHON_WHITESPACE_EDGES, "");
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function collapsePythonWhitespace(value: string): string {
  return trimPythonWhitespace(value).replace(PYTHON_WHITESPACE_RUN, " ");
}

export function caseFold(text: string): string {
  let result = "";
  for (const character of text) {
    const folded = CASE_FOLD_MAP[character];
    result += folded === undefined ? character : folded;
  }
  return result;
}

export function normalizeEntryText(text: string): NormalizedEntryText {
  const displayText = trimPythonWhitespace(text.normalize("NFKC"));
  if (displayText.length === 0) {
    return { status: EntryTextStatus.EMPTY };
  }
  if (codePointLength(displayText) > MAX_ENTRY_TEXT_LENGTH) {
    return { status: EntryTextStatus.TOO_LONG };
  }
  return {
    status: EntryTextStatus.VALID,
    displayText,
    normalizedText: caseFold(collapsePythonWhitespace(displayText)),
  };
}

function normalizeIdentityField(value: string): string {
  return caseFold(collapsePythonWhitespace(value.normalize("NFKC")));
}

export function normalizeSenseIdentity(
  partOfSpeech: string,
  definition: string,
): readonly [string, string] {
  return [normalizeIdentityField(partOfSpeech), normalizeIdentityField(definition)];
}

export function parseCaptureMessage(message: string): CaptureRequest | null {
  const stripped = trimPythonWhitespace(message);
  if (stripped.length === 0 || stripped.startsWith("/")) {
    return null;
  }

  const lines = stripped.split(PYTHON_LINE_BOUNDARY);
  const firstLine = lines[0];
  if (firstLine === undefined) {
    return null;
  }
  const normalized = normalizeEntryText(firstLine);
  if (normalized.status !== EntryTextStatus.VALID) {
    return null;
  }
  const contextText = trimPythonWhitespace(lines.slice(1).join("\n"));
  return {
    displayText: normalized.displayText,
    context: contextText.length === 0 ? null : contextText,
  };
}

function prepareSenseCard(value: unknown): SenseCard | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const candidate = value as Partial<Record<keyof SenseCard, unknown>>;
  if (
    typeof candidate.partOfSpeech !== "string" ||
    typeof candidate.definition !== "string" ||
    typeof candidate.exampleSentence !== "string"
  ) {
    return null;
  }

  const partOfSpeech = trimPythonWhitespace(candidate.partOfSpeech);
  const definition = trimPythonWhitespace(candidate.definition);
  const exampleSentence = trimPythonWhitespace(candidate.exampleSentence);
  if (
    codePointLength(partOfSpeech) === 0 ||
    codePointLength(partOfSpeech) > MAX_PART_OF_SPEECH_LENGTH ||
    codePointLength(definition) === 0 ||
    codePointLength(definition) > MAX_SENSE_TEXT_LENGTH ||
    codePointLength(exampleSentence) === 0 ||
    codePointLength(exampleSentence) > MAX_SENSE_TEXT_LENGTH
  ) {
    return null;
  }
  return { partOfSpeech, definition, exampleSentence };
}

export function validateSenseCards(value: unknown): SenseCardsValidation {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_SENSE_COUNT) {
    return { valid: false, reason: "count" };
  }

  const cards: SenseCard[] = [];
  for (const candidate of value) {
    const card = prepareSenseCard(candidate);
    if (card === null) {
      return { valid: false, reason: "card" };
    }
    cards.push(card);
  }

  const seen = new Set<string>();
  for (const card of cards) {
    const [partOfSpeech, definition] = normalizeSenseIdentity(
      card.partOfSpeech,
      card.definition,
    );
    const identity = `${partOfSpeech.length}:${partOfSpeech}${definition}`;
    if (seen.has(identity)) {
      return { valid: false, reason: "duplicate" };
    }
    seen.add(identity);
  }
  return { valid: true, cards };
}

function prepareSourceContext(value: unknown): string | null | undefined {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "string") {
    return undefined;
  }
  const context = trimPythonWhitespace(value);
  if (codePointLength(context) > MAX_SOURCE_CONTEXT_LENGTH) {
    return undefined;
  }
  return context.length === 0 ? null : context;
}

export function prepareCaptureCommand(command: CaptureCommand): PreparedCaptureCommand | null {
  if (!(command.operation instanceof CaptureOperation)) {
    return null;
  }
  const normalized = normalizeEntryText(command.displayText);
  if (normalized.status !== EntryTextStatus.VALID) {
    return null;
  }
  const sourceContext = prepareSourceContext(command.sourceContext);
  if (sourceContext === undefined) {
    return null;
  }

  if (command.operation === CaptureOperation.EXISTING_SENSE) {
    if (command.card != null || command.matchingSenseId == null) {
      return null;
    }
    return {
      displayText: normalized.displayText,
      normalizedText: normalized.normalizedText,
      operation: command.operation,
      card: null,
      sourceContext,
      matchingSenseId: command.matchingSenseId,
    };
  }

  if (
    (command.operation !== CaptureOperation.NEW_ENTRY &&
      command.operation !== CaptureOperation.NEW_SENSE) ||
    command.card == null ||
    command.matchingSenseId != null
  ) {
    return null;
  }
  const card = prepareSenseCard(command.card);
  if (card === null) {
    return null;
  }
  return {
    displayText: normalized.displayText,
    normalizedText: normalized.normalizedText,
    operation: command.operation,
    card,
    sourceContext,
    matchingSenseId: null,
  };
}
