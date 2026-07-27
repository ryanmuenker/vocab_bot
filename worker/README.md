# Cloudflare Worker vocabulary companion

An event-driven deployment of the same vocabulary companion that runs under the
Hermes gateway: a Telegram webhook, a `VocabularyCompanion` Durable Object
holding the SQLite study state, and a cron alarm that acts as the review ticker.

It implements the **same v5 model** as `src/hermes_vocab/`: embedded FSRS-6
scheduling, directional forward/reverse cards, the shared five-per-local-day
introduction quota, sibling burial, one tail retry, and `/review`,
`/test forward|reverse`, `/endstudy`.

Python under `src/hermes_vocab/` is the source of truth. When domain behavior
changes there, mirror it in `src/domain/` and `src/storage/vocabulary-store.ts`.

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
