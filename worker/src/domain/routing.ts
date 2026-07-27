import { caseFold, trimPythonWhitespace } from "./normalization";

const HINT_REQUESTS: Record<string, true> = {
  hint: true,
  "give me a hint": true,
  "can i have a hint": true,
  "show me an example": true,
  "example sentence": true,
};
const PYTHON_WHITESPACE_RUN = /[\p{White_Space}\u001c-\u001f]+/gu;

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

export function parseTestCommand(message: string): "test" | "usage" | "other" | "capture" {
  const name = slashCommandName(message);
  if (name === null) return message.startsWith("/") ? "capture" : "other";
  if (name !== "test") return "other";
  const firstWhitespace = message.search(/[\p{White_Space}\u001c-\u001f]/u);
  const argumentsText = firstWhitespace === -1 ? "" : trimPythonWhitespace(message.slice(firstWhitespace));
  return argumentsText.length === 0 ? "test" : "usage";
}
