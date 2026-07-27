import { env, exports } from "cloudflare:workers";
import { createScheduledController } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index";

function update(overrides: Record<string, unknown> = {}) {
  return {
    update_id: 1,
    message: {
      message_id: 1,
      text: "word",
      chat: { id: 123456, type: "private" },
      from: { id: 123456 },
      ...overrides,
    },
  };
}

function webhook(body: unknown, secret = "test-webhook-secret", headers: HeadersInit = {}) {
  return exports.default.fetch(new Request("https://example.test/telegram/webhook", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": secret,
      ...headers,
    },
    body: JSON.stringify(body),
  }));
}

describe("Worker HTTP and cron surface", () => {
  it("serves stateless health and rejects an invalid webhook secret", async () => {
    expect(await (await exports.default.fetch(new Request("https://example.test/healthz"))).json())
      .toEqual({ ok: true });
    expect((await webhook(update(), "wrong")).status).toBe(401);
  });

  it("silently ignores unsupported updates, wrong identities, topics, and non-test commands", async () => {
    expect((await webhook({ edited_message: update().message })).status).toBe(200);
    expect((await webhook(update({ chat: { id: 999, type: "private" } }))).status).toBe(200);
    expect((await webhook(update({ message_thread_id: 3 }))).status).toBe(200);
    expect((await webhook(update({ text: "/other" }))).status).toBe(200);
    expect(await env.VOCABULARY.getByName("123456").summary()).toMatchObject({ pendingInbox: 0 });
  });

  it("admits test commands and keeps slash-containing paths capturable", async () => {
    expect((await webhook(update({ text: "/test now" }))).status).toBe(200);
    expect((await webhook({ ...update({ text: "/tmp/vocabulary" }), update_id: 2 })).status).toBe(200);
    expect(await env.VOCABULARY.getByName("123456").summary()).toMatchObject({ pendingInbox: 2 });
    const blockedExport = await exports.default.fetch(new Request("https://example.test/admin/export", {
      headers: { Authorization: "Bearer test-admin-token" },
    }));
    expect(blockedExport.status).toBe(409);
  });

  it("rejects oversized or malformed webhook JSON generically", async () => {
    const oversized = await exports.default.fetch(new Request("https://example.test/telegram/webhook", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": "65537",
        "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret",
      },
      body: "{}",
    }));
    expect(oversized.status).toBe(413);
    const malformed = await exports.default.fetch(new Request("https://example.test/telegram/webhook", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret",
      },
      body: "{",
    }));
    expect(malformed.status).toBe(400);
  });

  it("rejects oversized admin imports separately from webhook limits", async () => {
    const oversized = await exports.default.fetch(new Request("https://example.test/admin/import", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": String(1_048_577),
        "Authorization": "Bearer test-admin-token",
      },
      body: "{}",
    }));
    expect(oversized.status).toBe(413);
  });

  it("protects admin routes and enqueues cron with scheduled-time dedupe", async () => {
    expect((await exports.default.fetch(new Request("https://example.test/admin/summary"))).status)
      .toBe(401);
    const summary = await exports.default.fetch(new Request("https://example.test/admin/summary", {
      headers: { Authorization: "Bearer test-admin-token" },
    }));
    expect(summary.status).toBe(200);
    const before = await env.VOCABULARY.getByName("123456").summary();
    const controller = createScheduledController({ scheduledTime: Date.parse("2026-07-23T04:00:00Z") });
    await worker.scheduled(controller, env);
    await worker.scheduled(controller, env);
    expect((await env.VOCABULARY.getByName("123456").summary()).pendingInbox)
      .toBe(before.pendingInbox + 1);
  });
});
