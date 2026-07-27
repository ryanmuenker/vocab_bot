import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          TELEGRAM_BOT_TOKEN: "test-token",
          TELEGRAM_WEBHOOK_SECRET: "test-webhook-secret",
          TELEGRAM_ALLOWED_CHAT_ID: "123456",
          TELEGRAM_ALLOWED_USER_ID: "123456",
          OPENCODE_API_KEY: "test-opencode-key",
          ADMIN_TOKEN: "test-admin-token",
        },
      },
    }),
  ],
});
