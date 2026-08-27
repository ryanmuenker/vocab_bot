import { EvaluationGrade } from "../domain/models";
import type { Evaluation, SenseCard, VisualIntent, VocabularyEntry } from "../domain/models";
import {
  MAX_PART_OF_SPEECH_LENGTH,
  MAX_SENSE_TEXT_LENGTH,
  normalizeSenseIdentity,
  trimPythonWhitespace,
} from "../domain/normalization";
import { validateVisualIntent } from "../domain/visual-enrichment";

export const DefinitionStatus = {
  FOUND: "found",
  NOT_FOUND: "not_found",
  INVALID_RESPONSE: "invalid_response",
  PROVIDER_ERROR: "provider_error",
} as const;
export type DefinitionStatus = (typeof DefinitionStatus)[keyof typeof DefinitionStatus];
export interface DefinitionResult {
  readonly status: DefinitionStatus;
  readonly cards: readonly SenseCard[];
  readonly visualIntent: VisualIntent | null;
}

export const EvaluationStatus = {
  VALID: "valid",
  INVALID_RESPONSE: "invalid_response",
  PROVIDER_ERROR: "provider_error",
  RATE_LIMITED: "rate_limited",
} as const;
export type EvaluationStatus = (typeof EvaluationStatus)[keyof typeof EvaluationStatus];
export interface EvaluationResult {
  readonly status: EvaluationStatus;
  readonly evaluation: Evaluation | null;
}

export const SHOW_ANSWER_FEEDBACK = "You chose to reveal the answer.";
const MAX_EVALUATION_FEEDBACK_LENGTH = 500;
const MAX_SENSES = 20;
const CHAT_RETRY_DELAYS_MS = [250, 1_000] as const;
const CHAT_MAX_ATTEMPTS = CHAT_RETRY_DELAYS_MS.length + 1;
const VALID_GRADE: Record<string, EvaluationGrade> = {
  [EvaluationGrade.CORRECT]: EvaluationGrade.CORRECT,
  [EvaluationGrade.PARTIAL]: EvaluationGrade.PARTIAL,
  [EvaluationGrade.INCORRECT]: EvaluationGrade.INCORRECT,
};

const DEFINITION_SYSTEM_PROMPT =
  "You are a focused English dictionary enrichment service. " +
  "Return JSON only. For a defined entry, return senses containing 1 to 20 senses. " +
  "List every credible distinct English sense for the supplied entry, including common, " +
  "literary, archaic, regional, and major technical senses. Order senses with the most " +
  "common meaning first. When there are more than three senses, make the first three " +
  "semantically distinct from one another while still prioritizing common meanings. " +
  "Exclude hyper-specialized jargon and do not split mere wording variants into separate " +
  "senses. Each sense must contain exactly part_of_speech, definition, and example_sentence. " +
  "Definitions must be concise and examples must demonstrate that sense. " +
  "Optionally include visual with exactly sense_index, category, query, and description " +
  "only when one zero-based sense_index is the clear dominant visual referent. Category " +
  "must be exactly plant, animal, architecture, object, material, place, garment, food, " +
  "vehicle, instrument, landform, or visual style. Query must be a short literal Commons " +
  "search phrase grounded in that sense, and description must concisely describe the image. " +
  "Omit visual for competing unrelated senses, low confidence, or medical/anatomy, sexual, " +
  "gore, injury, procedure, person/social-role, action, event, emotion, or abstract topics. " +
  "If the entry is not an English term or expression, return exactly {\"status\":\"not_found\"}.";

const EVALUATION_SYSTEM_PROMPT =
  "You evaluate an English vocabulary learner's answer against stored senses. " +
  "Return JSON only with exactly two top-level keys: grade and feedback. " +
  "Grade must be exactly correct, partial, or incorrect. Accept an accurate " +
  "semantic paraphrase as correct even when it shares no wording with the stored " +
  "definition. A response matching any one valid stored sense can be correct; do " +
  "not require the learner to enumerate every sense. Use partial for an incomplete " +
  "but directionally valid meaning and incorrect for an unrelated or wrong meaning. " +
  "Feedback must briefly explain the grade, must not be blank, and must be at most " +
  "500 characters.";

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function chatCompletionContent(payload: Record<string, unknown>): string | null {
  if (!Array.isArray(payload.choices)) return null;
  const choice = object(payload.choices[0]);
  const message = choice === null ? null : object(choice.message);
  return message !== null && typeof message.content === "string"
    ? message.content
    : null;
}

function responsesContent(payload: Record<string, unknown>): string | null {
  if (!Array.isArray(payload.output)) return null;
  for (const candidate of payload.output) {
    const output = object(candidate);
    if (output === null || output.type !== "message" || !Array.isArray(output.content)) continue;
    for (const candidateContent of output.content) {
      const content = object(candidateContent);
      if (content !== null && content.type === "output_text" && typeof content.text === "string") {
        return content.text;
      }
    }
  }
  return null;
}

export function parseDefinitionResponse(text: unknown, displayText: string): DefinitionResult {
  if (typeof text !== "string") {
    return { status: DefinitionStatus.INVALID_RESPONSE, cards: [], visualIntent: null };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { status: DefinitionStatus.INVALID_RESPONSE, cards: [], visualIntent: null };
  }
  const payload = object(parsed);
  if (payload === null) {
    return { status: DefinitionStatus.INVALID_RESPONSE, cards: [], visualIntent: null };
  }
  if (exactKeys(payload, ["status"]) && payload.status === "not_found") {
    return { status: DefinitionStatus.NOT_FOUND, cards: [], visualIntent: null };
  }
  const validTopLevel = exactKeys(payload, ["senses"]) || exactKeys(payload, ["senses", "visual"]);
  if (!validTopLevel || !Array.isArray(payload.senses) ||
      payload.senses.length < 1 || payload.senses.length > MAX_SENSES) {
    return { status: DefinitionStatus.INVALID_RESPONSE, cards: [], visualIntent: null };
  }
  const validated: SenseCard[] = [];
  for (const candidate of payload.senses) {
    const sense = object(candidate);
    if (sense === null || !exactKeys(sense, ["part_of_speech", "definition", "example_sentence"]) ||
        typeof sense.part_of_speech !== "string" || typeof sense.definition !== "string" ||
        typeof sense.example_sentence !== "string") {
      return { status: DefinitionStatus.INVALID_RESPONSE, cards: [], visualIntent: null };
    }
    const card = {
      partOfSpeech: trimPythonWhitespace(sense.part_of_speech),
      definition: trimPythonWhitespace(sense.definition),
      exampleSentence: trimPythonWhitespace(sense.example_sentence),
    };
    if (
      Array.from(card.partOfSpeech).length < 1 ||
      Array.from(card.partOfSpeech).length > MAX_PART_OF_SPEECH_LENGTH ||
      Array.from(card.definition).length < 1 ||
      Array.from(card.definition).length > MAX_SENSE_TEXT_LENGTH ||
      Array.from(card.exampleSentence).length < 1 ||
      Array.from(card.exampleSentence).length > MAX_SENSE_TEXT_LENGTH
    ) {
      return { status: DefinitionStatus.INVALID_RESPONSE, cards: [], visualIntent: null };
    }
    validated.push(card);
  }
  const cards: SenseCard[] = [];
  const seen = new Set<string>();
  for (const card of validated) {
    const [partOfSpeech, definition] = normalizeSenseIdentity(card.partOfSpeech, card.definition);
    const identity = `${partOfSpeech.length}:${partOfSpeech}${definition}`;
    if (!seen.has(identity)) {
      seen.add(identity);
      cards.push(card);
    }
  }
  return {
    status: DefinitionStatus.FOUND,
    cards,
    visualIntent: Object.hasOwn(payload, "visual")
      ? validateVisualIntent(displayText, cards, payload.visual)
      : null,
  };
}

export function parseEvaluationResponse(text: unknown): EvaluationResult {
  if (typeof text !== "string") return { status: EvaluationStatus.INVALID_RESPONSE, evaluation: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { status: EvaluationStatus.INVALID_RESPONSE, evaluation: null };
  }
  const payload = object(parsed);
  if (payload === null || !exactKeys(payload, ["grade", "feedback"]) ||
      typeof payload.grade !== "string" || typeof payload.feedback !== "string" ||
      !Object.hasOwn(VALID_GRADE, payload.grade)) {
    return { status: EvaluationStatus.INVALID_RESPONSE, evaluation: null };
  }
  const feedback = trimPythonWhitespace(payload.feedback);
  if (Array.from(feedback).length < 1 || Array.from(feedback).length > MAX_EVALUATION_FEEDBACK_LENGTH) {
    return { status: EvaluationStatus.INVALID_RESPONSE, evaluation: null };
  }
  return {
    status: EvaluationStatus.VALID,
    evaluation: { grade: VALID_GRADE[payload.grade]!, feedback },
  };
}

interface OpenCodeConfig {
  readonly apiKey: string;
  readonly baseUrl: string;
  readonly model: string;
}

type ChatResult =
  | { readonly kind: "content"; readonly content: string }
  | { readonly kind: "rate_limited" }
  | { readonly kind: "provider_error" };

export class OpenCodeAdapter {
  constructor(private readonly config: OpenCodeConfig) {}

  async defineEntry(displayText: string): Promise<DefinitionResult> {
    const content = await this.chat(
      [
        { role: "system", content: DEFINITION_SYSTEM_PROMPT },
        { role: "user", content: JSON.stringify({ display_text: displayText }) },
      ],
      4_000,
    );
    if (content.kind !== "content") {
      return { status: DefinitionStatus.PROVIDER_ERROR, cards: [], visualIntent: null };
    }
    const result = parseDefinitionResponse(content.content, displayText);
    if (result.status === DefinitionStatus.INVALID_RESPONSE) {
      console.warn({
        event: "opencode_definition_failure",
        kind: "invalid_response",
      });
    }
    return result;
  }

  async evaluateAnswer(entry: VocabularyEntry, answerText: string): Promise<EvaluationResult> {
    if (answerText === "show answer") {
      return {
        status: EvaluationStatus.VALID,
        evaluation: { grade: EvaluationGrade.INCORRECT, feedback: SHOW_ANSWER_FEEDBACK },
      };
    }
    if (trimPythonWhitespace(answerText).length === 0) {
      return { status: EvaluationStatus.INVALID_RESPONSE, evaluation: null };
    }
    const content = await this.chat(
      [
        { role: "system", content: EVALUATION_SYSTEM_PROMPT },
        {
          role: "user",
          content: JSON.stringify({
            display_text: entry.displayText,
            answer_text: answerText,
            senses: entry.senses.map((sense) => ({
              part_of_speech: sense.partOfSpeech,
              definition: sense.definition,
              example_sentence: sense.exampleSentence,
            })),
          }),
        },
      ],
      // The configured models reason before answering. Budget for reasoning and
      // output rather than only the two short JSON fields; a 500-token budget
      // truncated substantive evaluations while cheap nonsense answers passed.
      4_000,
    );
    if (content.kind === "rate_limited") {
      return { status: EvaluationStatus.RATE_LIMITED, evaluation: null };
    }
    return content.kind === "provider_error"
      ? { status: EvaluationStatus.PROVIDER_ERROR, evaluation: null }
      : parseEvaluationResponse(content.content);
  }

  private async chat(
    messages: readonly { readonly role: "system" | "user"; readonly content: string }[],
    maxTokens: number,
  ): Promise<ChatResult> {
    const responsesApi = this.config.model === "gpt-5.6-luna" ||
      this.config.model === "grok-4.5" ||
      this.config.model === "muse-spark-1.2-contributor";
    const requestBody = responsesApi
      ? JSON.stringify({
        model: this.config.model,
        instructions: messages[0]?.content ?? "",
        input: messages[1]?.content ?? "",
        max_output_tokens: maxTokens,
      })
      : JSON.stringify({
        model: this.config.model,
        messages,
        max_tokens: maxTokens,
        temperature: 0,
        tools: [],
      });
    for (let attempt = 0; attempt < CHAT_MAX_ATTEMPTS; attempt += 1) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 60_000);
      try {
        const response = await fetch(
          `${this.config.baseUrl}/${responsesApi ? "responses" : "chat/completions"}`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${this.config.apiKey}`,
              "Content-Type": "application/json",
            },
            body: requestBody,
            signal: controller.signal,
          },
        );
        if (response.ok) {
          let responseBody: unknown;
          try {
            responseBody = await response.json();
          } catch (error) {
            if (error instanceof SyntaxError) return { kind: "provider_error" };
            throw error;
          }
          const payload = object(responseBody);
          if (payload === null) return { kind: "provider_error" };
          const content = responsesApi
            ? responsesContent(payload)
            : chatCompletionContent(payload);
          return content === null
            ? { kind: "provider_error" }
            : { kind: "content", content };
        }
        console.warn({
          event: "opencode_chat_failure",
          kind: "http",
          status: response.status,
          attempt: attempt + 1,
          maxAttempts: CHAT_MAX_ATTEMPTS,
        });
        if (response.status !== 429 && response.status < 500) {
          return { kind: "provider_error" };
        }
        if (attempt === CHAT_MAX_ATTEMPTS - 1) {
          return response.status === 429
            ? { kind: "rate_limited" }
            : { kind: "provider_error" };
        }
      } catch {
        console.warn({
          event: "opencode_chat_failure",
          kind: "network",
          attempt: attempt + 1,
          maxAttempts: CHAT_MAX_ATTEMPTS,
        });
        if (attempt === CHAT_MAX_ATTEMPTS - 1) return { kind: "provider_error" };
      } finally {
        clearTimeout(timeout);
      }
      const delay = CHAT_RETRY_DELAYS_MS[attempt];
      if (delay !== undefined) await scheduler.wait(delay);
    }
    return { kind: "provider_error" };
  }
}
