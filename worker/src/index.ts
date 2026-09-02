import { parseStudyCommand, slashCommandName } from "./domain/routing";
import { EntryTextStatus } from "./domain/models";
import { normalizeEntryText } from "./domain/normalization";
import { parseVerifiedEnvelope, sha256Snapshot } from "./domain/snapshot";
import { TelegramAdapter } from "./integrations/telegram";
import { INSPECTOR_HTML } from "./inspector-page";

export { VocabularyCompanion } from "./vocabulary-companion";

export type AdminEnv = Env & { ADMIN_TOKEN?: string };

const MAX_BODY_BYTES = 65_536;
const MAX_IMPORT_BYTES = 1_048_576;
const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" } as const;
const INSPECTOR_JSON_HEADERS = {
  ...JSON_HEADERS,
  "Cache-Control": "no-store",
} as const;
const INSPECTOR_HEADERS = {
  "Content-Type": "text/html; charset=utf-8",
  "Cache-Control": "no-store",
  "Content-Security-Policy":
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; " +
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
} as const;

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: JSON_HEADERS });
}

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

async function timingSafeEqual(actual: string | null, expected: string): Promise<boolean> {
  if (actual === null) return false;
  const encoder = new TextEncoder();
  const [actualHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(actual)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const left = new Uint8Array(actualHash);
  const right = new Uint8Array(expectedHash);
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left[index]! ^ right[index]!;
  return difference === 0;
}

async function readJson(request: Request, maxBytes = MAX_BODY_BYTES): Promise<unknown> {
  const contentType = request.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (contentType !== "application/json") throw new TypeError("invalid content type");
  const declaredLength = request.headers.get("Content-Length");
  if (declaredLength !== null) {
    const parsed = Number(declaredLength);
    if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > maxBytes) {
      throw new RangeError("body too large");
    }
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > maxBytes) throw new RangeError("body too large");
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new TypeError("invalid JSON");
  }
}

function owner(env: Env) {
  return env.VOCABULARY.getByName(env.TELEGRAM_ALLOWED_CHAT_ID);
}

function telegramMessage(value: unknown, env: Env) {
  const update = object(value);
  const message = update === null ? null : object(update.message);
  const chat = message === null ? null : object(message.chat);
  const sender = message === null ? null : object(message.from);
  if (
    update === null ||
    !Number.isSafeInteger(update.update_id) ||
    message === null ||
    !Number.isSafeInteger(message.message_id) ||
    typeof message.text !== "string" ||
    chat === null ||
    chat.type !== "private" ||
    sender === null ||
    message.message_thread_id !== undefined ||
    String(chat.id) !== env.TELEGRAM_ALLOWED_CHAT_ID ||
    String(sender.id) !== env.TELEGRAM_ALLOWED_USER_ID
  ) return null;
  return {
    updateId: update.update_id as number,
    messageId: message.message_id as number,
    chatId: String(chat.id),
    senderId: String(sender.id),
    text: message.text,
    receivedAt: new Date().toISOString(),
  };
}

async function webhook(request: Request, env: Env): Promise<Response> {
  if (!(await timingSafeEqual(
    request.headers.get("X-Telegram-Bot-Api-Secret-Token"),
    env.TELEGRAM_WEBHOOK_SECRET,
  ))) return json({ error: "unauthorized" }, 401);

  let payload: unknown;
  try {
    payload = await readJson(request);
  } catch (error) {
    return json({ error: error instanceof RangeError ? "payload too large" : "invalid request" },
      error instanceof RangeError ? 413 : 400);
  }
  const admitted = telegramMessage(payload, env);
  if (admitted === null) return new Response(null, { status: 200 });
  // Unknown slash commands are ignored; anything else is capturable text.
  const commandName = slashCommandName(admitted.text);
  if (commandName !== null && parseStudyCommand(admitted.text) === null) {
    return new Response(null, { status: 200 });
  }
  await owner(env).enqueueTelegramUpdate(admitted);
  return new Response(null, { status: 200 });
}

async function admin(request: Request, env: AdminEnv, pathname: string): Promise<Response> {
  if (env.ADMIN_TOKEN === undefined) return new Response(null, { status: 404 });
  if (!(await timingSafeEqual(
    request.headers.get("Authorization"),
    `Bearer ${env.ADMIN_TOKEN}`,
  ))) return json({ error: "unauthorized" }, 401);

  try {
    if (request.method === "POST" && pathname === "/admin/import") {
      const envelope = await parseVerifiedEnvelope(await readJson(request, MAX_IMPORT_BYTES));
      if (envelope === null) return json({ error: "invalid request" }, 400);
      return json(await owner(env).importSnapshot(envelope.snapshot));
    }
    if (request.method === "GET" && pathname === "/admin/export") {
      const snapshot = await owner(env).exportSnapshot();
      if (snapshot === null) return json({ error: "inbox work is unfinished" }, 409);
      return json({ sha256: await sha256Snapshot(snapshot), snapshot });
    }
    if (request.method === "GET" && pathname === "/admin/summary") {
      return json(await owner(env).summary());
    }
    if (request.method === "GET" && pathname === "/admin/image-backfill") {
      return json(await owner(env).imageBackfillStatus());
    }
    if (request.method === "POST" && pathname === "/admin/backfill-images") {
      let value: unknown;
      try {
        value = await readJson(request);
      } catch (error) {
        return json(
          { error: error instanceof RangeError ? "payload too large" : "invalid request" },
          error instanceof RangeError ? 413 : 400,
        );
      }
      const body = object(value);
      if (
        body === null ||
        !exactKeys(body, ["limit", "retryFailures"]) ||
        !Number.isSafeInteger(body.limit) ||
        (body.limit as number) < 1 ||
        (body.limit as number) > 10 ||
        typeof body.retryFailures !== "boolean"
      ) {
        return json({ error: "invalid request" }, 400);
      }
      return json(await owner(env).backfillImages({
        limit: body.limit as number,
        retryFailures: body.retryFailures,
      }));
    }
    if (request.method === "GET" && pathname === "/admin/inspector-data") {
      return new Response(JSON.stringify(await owner(env).inspectorData()), {
        headers: INSPECTOR_JSON_HEADERS,
      });
    }
    if (request.method === "POST" && pathname === "/admin/provider-smoke") {
      const body = object(await readJson(request));
      if (body === null || !exactKeys(body, ["displayText"]) ||
          typeof body.displayText !== "string" || body.displayText.trim().length === 0) {
        return json({ error: "invalid request" }, 400);
      }
      return json(await owner(env).providerSmoke(body.displayText));
    }
    if (request.method === "POST" && pathname === "/admin/fix-entry") {
      const body = object(await readJson(request));
      const normalized =
        body !== null && typeof body.displayText === "string"
          ? normalizeEntryText(body.displayText)
          : null;
      if (
        body === null ||
        !exactKeys(body, ["id", "displayText"]) ||
        !Number.isSafeInteger(body.id) ||
        (body.id as number) <= 0 ||
        normalized?.status !== EntryTextStatus.VALID
      ) {
        return json({ error: "invalid request" }, 400);
      }
      const result = await owner(env).fixEntry({
        id: body.id as number,
        displayText: normalized.displayText,
      });
      if (result.status === "not_found") return json({ error: "entry not found" }, 404);
      if (result.status === "conflict") {
        return json({ error: "another entry already uses that text" }, 409);
      }
      return json(result);
    }
    if (request.method === "POST" && pathname === "/admin/delete-entries") {
      const body = object(await readJson(request));
      if (
        body === null ||
        !exactKeys(body, ["displayTexts"]) ||
        !Array.isArray(body.displayTexts) ||
        body.displayTexts.length < 1 ||
        body.displayTexts.length > 100
      ) {
        return json({ error: "invalid request" }, 400);
      }
      const normalizedTexts: string[] = [];
      const seen = new Set<string>();
      for (const displayText of body.displayTexts) {
        if (typeof displayText !== "string") return json({ error: "invalid request" }, 400);
        const normalized = normalizeEntryText(displayText);
        if (normalized.status !== EntryTextStatus.VALID) {
          return json({ error: "invalid request" }, 400);
        }
        if (!seen.has(normalized.normalizedText)) {
          seen.add(normalized.normalizedText);
          normalizedTexts.push(normalized.normalizedText);
        }
      }
      return json(await owner(env).deleteEntries({ normalizedTexts }));
    }
    if (request.method === "POST" && pathname === "/admin/send-smoke") {
      const contentLength = request.headers.get("Content-Length");
      if (contentLength !== null && Number(contentLength) > 0) return json({ error: "invalid request" }, 400);
      await new TelegramAdapter({
        botToken: env.TELEGRAM_BOT_TOKEN,
        chatId: env.TELEGRAM_ALLOWED_CHAT_ID,
      }).sendText("Cloudflare vocabulary deployment check.");
      return json({ ok: true });
    }
    if (request.method === "POST" && pathname === "/admin/run-daily-review") {
      const body = object(await readJson(request));
      if (body === null || !exactKeys(body, Object.hasOwn(body, "nowUtc") ? ["nowUtc"] : [])) {
        return json({ error: "invalid request" }, 400);
      }
      const nowUtc = Object.hasOwn(body, "nowUtc") ? body.nowUtc : new Date().toISOString();
      if (typeof nowUtc !== "string" || !/^\d{4}-\d{2}-\d{2}T.*Z$/u.test(nowUtc) ||
          Number.isNaN(Date.parse(nowUtc))) return json({ error: "invalid request" }, 400);
      return json(await owner(env).enqueueDailyReview({
        dedupeKey: `manual-review:${crypto.randomUUID()}`,
        nowUtc,
      }));
    }
  } catch (error) {
    return json(
      { error: error instanceof RangeError ? "payload too large" : "request failed" },
      error instanceof RangeError ? 413 : 500,
    );
  }
  return new Response(null, { status: 404 });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/healthz") return json({ ok: true });
    if (request.method === "GET" && url.pathname === "/inspector") {
      if ((env as AdminEnv).ADMIN_TOKEN === undefined) return new Response(null, { status: 404 });
      return new Response(INSPECTOR_HTML, { headers: INSPECTOR_HEADERS });
    }
    if (request.method === "POST" && url.pathname === "/telegram/webhook") {
      return webhook(request, env);
    }
    if (url.pathname.startsWith("/admin/")) return admin(request, env as AdminEnv, url.pathname);
    return new Response(null, { status: 404 });
  },

  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    await owner(env).enqueueDailyReview({
      dedupeKey: `cron:${controller.scheduledTime}`,
      nowUtc: new Date(controller.scheduledTime).toISOString(),
    });
  },
} satisfies ExportedHandler<Env>;
