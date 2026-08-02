# Cloudflare Worker vocabulary companion

An event-driven deployment of the same vocabulary companion that runs under the
Hermes gateway: a Telegram webhook, a `VocabularyCompanion` Durable Object
holding the SQLite study state, and a cron alarm that acts as the review ticker.

It implements the **same v5 model** as `src/hermes_vocab/`: embedded FSRS-6
scheduling, directional forward/reverse cards, `/review`'s five-per-local-day
introduction quota, unseen-only directional tests, sibling burial, one tail
retry, and `/review`, `/test forward|reverse`, `/endstudy`.

The Worker implementation under `worker/src/` is the production source of truth. User-visible fixes must land here first and be covered under `worker/test/`. Python is secondary migration/reference code; update it only when snapshot compatibility, offline tooling, or deliberately maintained parity requires it.

## Delivery safety

A Worker sees Telegram's HTTP response inline, so it needs no receipt hook. The
safety property is the same as the Hermes path:

1. the prompt is persisted `prepared` before anything is sent;
2. it is promoted to `delivered` — and only then answerable — after every chunk
   lands, recording the message IDs and a fingerprint of the text actually sent;
3. a failed send records a `failed` attempt and leaves the prompt `prepared`, so
   it stays retryable and can never consume the next message you type.

## Commands

```bash
npm test           # vitest against workerd
npm run typecheck  # wrangler types, then tsc --noEmit
npx wrangler dev   # local runtime on :8787
npx wrangler deploy
```

## Configuration

Non-secret values live in `wrangler.jsonc` under `vars`: `OPENCODE_BASE_URL`,
`OPENCODE_MODEL`, `HERMES_TIMEZONE`, and `HERMES_REVIEW_HOUR` (the local hour
before which the ticker stays silent unless an older overdue backlog exists).

Secrets, set with `wrangler secret put <NAME>`:

| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | outbound sends |
| `TELEGRAM_WEBHOOK_SECRET` | verifies `X-Telegram-Bot-Api-Secret-Token` |
| `TELEGRAM_ALLOWED_CHAT_ID` | the only chat that may drive study state |
| `TELEGRAM_ALLOWED_USER_ID` | the only sender that may drive study state |
| `OPENCODE_API_KEY` | definition and evaluation provider |
| `ADMIN_TOKEN` | enables `/admin/*`; when unset those routes return 404 |

### Local development trap

`wrangler dev` injects only the keys the generated `Env` knows about. A
`.dev.vars` entry that is not part of that surface — `ADMIN_TOKEN` is the one
that matters — is **silently not injected**, so `/admin/*` answers `404` locally
even with the value present in `.dev.vars`. Verified against
workerd@1.20260721.1: `TELEGRAM_BOT_TOKEN` loads, `ADMIN_TOKEN` does not.

Consequence: the snapshot import below cannot be rehearsed with `wrangler dev`
as configured. Rehearse it against a preview deployment with the secret set, and
confirm `/admin/summary` returns JSON before trusting an import.

The same shape bites in production: forget `wrangler secret put ADMIN_TOKEN` and
the admin surface is not broken, it is absent.

## Migrating an existing library

`scripts/export_cloudflare.py` and `scripts/import_cloudflare.py` move a v5
SQLite database in and out of the Worker. The envelope is snapshot format
version 2 and carries entries, senses, cards, study sessions, queue, prompts,
delivery attempts, answer drafts, review attempts, and the preserved legacy
audit rows, under a sha256 digest that both runtimes compute identically.

```bash
# From the repository root, against a quiesced database.
.venv/bin/python scripts/export_cloudflare.py \
  --database ~/.local/share/hermes-vocab/vocabulary.sqlite3 \
  --output /tmp/vocab-snapshot.json

curl -X POST https://<worker-host>/admin/import \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  --data @/tmp/vocab-snapshot.json
```

Import refuses a non-empty inbox and rejects a digest mismatch. Stop the Hermes
gateway and every cron or ticker process before exporting; the two deployments
must never run against the same library at once, because each is an independent
scheduling authority.

## Cutover: the Hermes gateway will steal the chat back

Telegram delivers each update to a webhook **or** to long-polling, never both,
so the Worker and the Hermes gateway cannot share one bot. Handing the bot to
the Worker takes more than `setWebhook`, because the Telegram adapter calls
`_delete_webhook_best_effort()` while bootstrapping polling
(`plugins/platforms/telegram/adapter.py`). Every gateway start — including the
automatic launchd start after a reboot — therefore **deletes the Worker's
webhook and silently resumes polling**, and the cutover appears to work right
up until the next restart.

The adapter is enabled purely by a non-empty `TELEGRAM_BOT_TOKEN`, so a durable
cutover is:

1. `hermes cron pause <daily-review-job-id>` — stop the second ticker.
2. `hermes plugins disable vocabulary` — stop the second scheduling authority.
3. Comment out `TELEGRAM_BOT_TOKEN` in `~/.hermes/.env` — stop the webhook theft.
4. `hermes gateway restart`, then export, import, and `setWebhook` with a
   `secret_token` that matches the Worker's `TELEGRAM_WEBHOOK_SECRET`.

Reverting is the same list backwards, and step 3 does most of the work on its
own: restoring the token makes the gateway delete the webhook at startup, which
is exactly the handover in reverse. Export from `/admin/export` first — after
cutover the Durable Object is the source of truth and the local SQLite file is
a stale backup.
