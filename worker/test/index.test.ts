import { env, exports } from "cloudflare:workers";
import { createScheduledController } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index";
import type { AdminEnv } from "../src/index";

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

  it("silently ignores unsupported updates, wrong identities, topics, and unknown commands", async () => {
    expect((await webhook({ edited_message: update().message })).status).toBe(200);
    expect((await webhook(update({ chat: { id: 999, type: "private" } }))).status).toBe(200);
    expect((await webhook(update({ message_thread_id: 3 }))).status).toBe(200);
    expect((await webhook(update({ text: "/other" }))).status).toBe(200);
    expect(await env.VOCABULARY.getByName("123456").summary()).toMatchObject({ pendingInbox: 0 });
  });

  it("admits every study command and keeps slash-containing paths capturable", async () => {
    expect((await webhook(update({ text: "/test forward" }))).status).toBe(200);
    expect((await webhook({ ...update({ text: "/review" }), update_id: 2 })).status).toBe(200);
    expect((await webhook({ ...update({ text: "/endstudy" }), update_id: 3 })).status).toBe(200);
    expect((await webhook({ ...update({ text: "/tmp/vocabulary" }), update_id: 4 })).status).toBe(200);
    expect(await env.VOCABULARY.getByName("123456").summary()).toMatchObject({ pendingInbox: 4 });
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

  it("rejects an admin import whose digest does not match its snapshot", async () => {
    const snapshot = {
      formatVersion: 2,
      entries: [],
      senses: [],
      reviewEvents: [],
      testSessions: [],
      testQuestions: [],
      cards: [],
      studySessions: [],
      studyQueue: [],
      studyPrompts: [],
      deliveryAttempts: [],
      answerDrafts: [],
      reviewAttempts: [],
    };
    const rejected = await exports.default.fetch(new Request("https://example.test/admin/import", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer test-admin-token",
      },
      body: JSON.stringify({ sha256: "0".repeat(64), snapshot }),
    }));
    expect(rejected.status).toBe(400);
    expect(await rejected.json()).toEqual({ error: "invalid request" });
  });

  it("protects admin routes and keeps the cron tick silent with nothing due", async () => {
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
    // An empty library has nothing to ask about, so the tick enqueues nothing.
    expect((await env.VOCABULARY.getByName("123456").summary()).pendingInbox)
      .toBe(before.pendingInbox);
  });

  it("serves the vocabulary inspector and protects its live data", async () => {
    const shell = await exports.default.fetch(new Request("https://example.test/inspector"));
    expect(shell.status).toBe(200);
    expect(shell.headers.get("Content-Type")).toBe("text/html; charset=utf-8");
    expect(shell.headers.get("Cache-Control")).toBe("no-store");
    expect(shell.headers.get("Content-Security-Policy")).toContain("default-src 'none'");
    const html = await shell.text();
    expect(html).toContain('data-inspector');
    expect(html).toContain('id="token-form"');
    expect(html).toContain('id="atlas-view"');
    expect(html).toContain('id="table-view"');
    expect(html).toContain("Delete entry");
    expect(html).toContain("/admin/delete-entries");
    expect(html).toContain("This cannot be undone.");
    expect(html).not.toMatch(/<script[^>]+src=/u);
    expect(html).not.toMatch(/<link[^>]+href=/u);

    const missingAdminEnv = new Proxy(env, {
      get(target, property, receiver) {
        return property === "ADMIN_TOKEN" ? undefined : Reflect.get(target, property, receiver);
      },
    }) as AdminEnv;
    expect((await worker.fetch(
      new Request("https://example.test/inspector"),
      missingAdminEnv,
    )).status).toBe(404);
    expect((await exports.default.fetch(new Request("https://example.test/inspector", {
      method: "POST",
    }))).status).toBe(404);

    expect((await exports.default.fetch(
      new Request("https://example.test/admin/inspector-data"),
    )).status).toBe(401);
    const dataResponse = await exports.default.fetch(
      new Request("https://example.test/admin/inspector-data", {
        headers: { Authorization: "Bearer test-admin-token" },
      }),
    );
    expect(dataResponse.status).toBe(200);
    expect(dataResponse.headers.get("Content-Type")).toBe("application/json; charset=utf-8");
    expect(dataResponse.headers.get("Cache-Control")).toBe("no-store");
    expect(await dataResponse.json()).toMatchObject({
      generatedAt: expect.any(String),
      memorizedStabilityDays: 30,
      summary: { total: 0, unseen: 0, learning: 0, memorized: 0, due: 0 },
      entries: [],
    });
  });
});
