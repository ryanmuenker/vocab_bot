import { exports } from "cloudflare:workers";
import { describe, expect, it } from "vitest";

const ADMIN_HEADERS = {
  "Authorization": "Bearer test-admin-token",
  "Content-Type": "application/json",
} as const;

function backfillImages(body: unknown): Promise<Response> {
  return exports.default.fetch(new Request("https://example.test/admin/backfill-images", {
    method: "POST",
    headers: ADMIN_HEADERS,
    body: JSON.stringify(body),
  }));
}

describe("admin image backfill routes", () => {
  it("requires admin authentication for status and mutations", async () => {
    const status = await exports.default.fetch(
      new Request("https://example.test/admin/image-backfill"),
    );
    expect(status.status).toBe(401);

    const mutation = await exports.default.fetch(
      new Request("https://example.test/admin/backfill-images", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 1, retryFailures: false }),
      }),
    );
    expect(mutation.status).toBe(401);
  });

  it("accepts only the exact backfill request body", async () => {
    for (const body of [
      {},
      { limit: 1 },
      { retryFailures: false },
      { limit: 1, retryFailures: false, extra: true },
      { limit: 0, retryFailures: false },
      { limit: 11, retryFailures: false },
      { limit: 1.5, retryFailures: false },
      { limit: Number.MAX_SAFE_INTEGER + 1, retryFailures: false },
      { limit: "1", retryFailures: false },
      { limit: 1, retryFailures: 0 },
      { limit: 1, retryFailures: "false" },
      null,
      [],
    ]) {
      const response = await backfillImages(body);
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "invalid request" });
    }

    for (const request of [
      new Request("https://example.test/admin/backfill-images", {
        method: "POST",
        headers: { ...ADMIN_HEADERS, "Content-Type": "text/plain" },
        body: JSON.stringify({ limit: 1, retryFailures: false }),
      }),
      new Request("https://example.test/admin/backfill-images", {
        method: "POST",
        headers: { "Authorization": "Bearer test-admin-token" },
        body: JSON.stringify({ limit: 1, retryFailures: false }),
      }),
      new Request("https://example.test/admin/backfill-images", {
        method: "POST",
        headers: ADMIN_HEADERS,
        body: "not json",
      }),
    ]) {
      const response = await exports.default.fetch(request);

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "invalid request" });
    }
  });
  it("returns the companion RPC results", async () => {
    const expectedStatus = {
      totalEntries: 0,
      associatedEntries: 0,
      neverAttemptedEntries: 0,
      attempts: {
        no_visual: 0,
        provider_error: 0,
        rate_limited: 0,
        invalid_response: 0,
        image_unavailable: 0,
      },
    };
    const status = await exports.default.fetch(
      new Request("https://example.test/admin/image-backfill", {
        headers: { "Authorization": "Bearer test-admin-token" },
      }),
    );
    expect(status.status).toBe(200);
    expect(status.headers.get("Content-Type")).toBe("application/json; charset=utf-8");
    expect(await status.json()).toEqual(expectedStatus);

    for (const body of [
      { limit: 1, retryFailures: false },
      { limit: 10, retryFailures: true },
    ]) {
      const mutation = await backfillImages(body);
      expect(mutation.status).toBe(200);
      expect(mutation.headers.get("Content-Type")).toBe("application/json; charset=utf-8");
      expect(await mutation.json()).toEqual({
        processed: 0,
        associated: 0,
        failed: 0,
        status: expectedStatus,
      });
    }
  });
});
