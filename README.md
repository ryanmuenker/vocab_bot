# Hermes Vocabulary Companion

A local-first vocabulary capture and daily-review capability for [Hermes Agent](https://hermes-agent.nousresearch.com/). Telegram and model inference stay in Hermes; vocabulary state and business rules stay in this package and its SQLite database.

Tested with Hermes Agent `0.18.2` on macOS.

## Architecture

```text
configured Telegram root DM
        │
        ▼
Hermes auth + command precedence
        │
        ▼
gateway_inbound_intercept
        │
        ├── pending review ──> ReviewService ──> exact answer
        ├── stored entry ────> SQLite ─────────> exact aggregate
        └── unseen entry ────> one auxiliary request
                                      │
                                      ▼
                              atomic CaptureService
                                      │
                                      ▼
                                   SQLite

Hermes no-agent cron ── scripts/daily_review.py ── ReviewService ── SQLite
          │
          └── exact stdout delivered to the Telegram home DM
```

SQLite is the only source of truth for saved entries and review state. Hermes transcripts, memory, cron metadata, and cron output may contain copies, but none decides whether an entry exists or which review is pending.

### Why each component exists

- `src/hermes_vocab/capture.py`: Unicode-aware entry normalization, duplicate handling, and atomic multi-sense saves. It has no Hermes or Telegram dependency.
- `src/hermes_vocab/review.py`: daily selection and pending/missed/answered transitions. It has no scheduler or transport dependency.
- `src/hermes_vocab/database.py`: private data-directory checks, migrations, SQLite connections, WAL, and transaction policy.
- `src/hermes_vocab/models.py`: small immutable domain dataclasses and enums; no ORM.
- `src/hermes_vocab/formatting.py`: exact user-facing aggregate and review text. Success text is emitted only after a committed save.
- `src/hermes_vocab/config.py`: database path, explicit IANA timezone, and optional dedicated Telegram chat resolution.
- `src/hermes_vocab/migrations/`: append-only schema migrations owned by this package.
- `src/hermes_vocab/hermes_plugin/`: the only Hermes-coupled layer: tools, contextual guidance, a focused definition provider, and deterministic gateway routing.
- `scripts/daily_review.py`: a thin deterministic cron entry point. Empty stdout means no Telegram message.
- `tests/`: domain and real-SQLite contracts plus standalone plugin registration seams.

### Deliberate constraints

- A gateway interceptor, not only a prompt skill: prompts cannot safely own transactions, uniqueness, review lifecycle state, or prevent general-agent consumers from claiming a dedicated message.
- A separate SQLite database, not Hermes' session database: ownership, migrations, backup, and restoration remain clear.
- One focused auxiliary definition request for an unseen entry: the configured DM never starts a general Hermes turn for vocabulary capture. Definitions are model-generated and may be inaccurate; verify unusual or high-stakes uses.
- No spaced repetition, tags, grading, or mandatory follow-up. The oldest never-reviewed entry is chosen first, then the least recently reviewed.
- A pending review owns the next non-command message in the configured root DM. Answer it or ask for the answer before capturing another entry.
- A no-agent cron job asks the morning question. This avoids model cost and guarantees `What does '<entry>' mean?` exactly.

## Capture and review behavior

In the root Telegram DM selected by `HERMES_VOCAB_TELEGRAM_CHAT_ID`, the complete trimmed non-command message is one entry or expression:

```text
pro forma
```

Phrases are first-class entries; there is no word parser, tagging step, or context syntax in this dedicated inbox. Processing order is fixed:

1. A pending review consumes the complete message as its answer.
2. A stored entry returns every SQLite sense in insertion order with `Already saved.` and makes no model request or write.
3. An unseen entry makes one focused auxiliary request for all credible senses. The complete validated set is committed in one SQLite transaction and returned with `✓ Saved.`.

Equivalent whitespace and case share one lookup key, while the first successfully captured display form is preserved. Concurrent requests converge on one complete aggregate. Exact duplicate generated `(part_of_speech, definition)` pairs collapse before persistence; malformed or partial provider output saves nothing.

Slash commands, non-configured chats, and Telegram topic lanes retain normal Hermes behavior. On non-dedicated conversational Telegram surfaces, the legacy first-line entry plus optional second-line context workflow remains available through `pre_llm_call`; context syntax does not apply in the dedicated DM.

Daily review remains entry-level: it asks one `What does '<entry>' mean?` question, then reveals all stored senses in capture order after an answer or `show answer`. Multiple senses are numbered. After completion, another review run on the same local day is silent.

## Current workstation status

Already completed on this machine:

- Hermes Agent `0.18.2` installed at `~/.hermes/hermes-agent`.
- The distributable vocabulary package installed into Hermes' Python environment.
- `vocabulary` entry-point plugin and Telegram toolset enabled.
- `HERMES_TIMEZONE=Asia/Kuala_Lumpur`, the default database path, and the dedicated Telegram root DM configured in `~/.hermes/.env`.
- `~/.hermes/scripts/daily_review.py` installed.
- Telegram tool progress and interim assistant messages disabled.
- `cron.wrap_response` disabled.
- `daily-vocabulary-review` scheduled once for `12:00` local time.

## Setup from a clean machine

### 1. Install Hermes

Use the official installer and complete its provider setup:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes --version
hermes setup
```

The browser engine is unrelated to vocabulary. If it is slow or unwanted, install with `--skip-browser` and add it later only if another capability needs it.

### 2. Install and enable this plugin

From this repository:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python -e .
hermes plugins enable vocabulary
hermes plugins list --plain --no-bundled
hermes tools list --platform telegram
```

Expected discovery:

```text
enabled      entrypoint 0.1.0    vocabulary
...
✓ enabled  vocabulary  🔌 Vocabulary
```

Configure the package in `~/.hermes/.env`:

```bash
HERMES_TIMEZONE=Asia/Kuala_Lumpur
HERMES_VOCAB_DB=~/.local/share/hermes-vocab/vocabulary.sqlite3
HERMES_VOCAB_TELEGRAM_CHAT_ID=<your numeric private DM chat ID>
```

Use your actual IANA timezone and allowlisted private Telegram DM ID; replace the angle-bracket placeholder rather than copying it literally. If Telegram is not configured yet, omit `HERMES_VOCAB_TELEGRAM_CHAT_ID` until Step 3. Leaving it unset disables only deterministic DM routing; tools, review cron, skill registration, and contextual guidance still load. Keep `~/.hermes/.env` permissioned to the current user; it also contains the Telegram token.

Suppress tool progress and interim assistant output in Telegram:

```bash
hermes config set display.platforms.telegram.tool_progress off
hermes config set display.platforms.telegram.interim_assistant_messages off
```

Confirm both values are `false` under `display.platforms.telegram` in `~/.hermes/config.yaml`.

A plugin-load smoke check that does not call a model:

```bash
HERMES_PLUGINS_DEBUG=1 hermes prompt-size
hermes plugins list --plain --no-bundled
hermes tools list --platform telegram
```

Before setting the chat ID, look for two vocabulary tools, the bundled skill, `pre_llm_call`, and the `vocabulary_definition` auxiliary task; `gateway_inbound_intercept` is intentionally absent.

### 3. Create and connect the Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`, choose the bot name and username, and copy the token.
3. Get your numeric Telegram user ID from `@userinfobot`.
4. Run:

   ```bash
   hermes gateway setup
   ```

5. Select Telegram, enter the bot token, and allowlist only your numeric user ID.
6. Add the numeric ID as `HERMES_VOCAB_TELEGRAM_CHAT_ID`, rerun the smoke commands above, and confirm `gateway_inbound_intercept` now registers.

Equivalent manual values in `~/.hermes/.env` are:

```bash
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_ALLOWED_USERS=<your numeric user ID>
```

Do not commit or paste the token into a prompt. If it leaks, use BotFather's `/revoke` immediately. Polling is the default and is appropriate for this local deployment; no webhook is needed.

### 4. Test the private DM and set it as home

Run the gateway in the foreground first:

```bash
hermes gateway
```

In the bot's configured private root DM:

1. Send `/sethome`. The DM chat ID is normally the same as your user ID.
2. Send a stored entry and confirm it returns directly from SQLite with `Already saved.`.
3. Send an unseen phrase such as `pro forma`; confirm one numbered aggregate ends in `✓ Saved.`.
4. Repeat it with different case/whitespace; confirm the committed aggregate returns with `Already saved.` and no model request.
5. Send `/status`; confirm ordinary Hermes command behavior.

Stop the foreground process after this check, then install the persistent user service:

```bash
hermes gateway install --start-now --start-on-login
hermes gateway status
```

The local machine and gateway must be running for capture and scheduled delivery. A later move to a small VPS changes deployment and paths, not the business logic.

### 5. Install and verify the morning review

Install the wrapper under Hermes' allowed scripts directory:

```bash
mkdir -p ~/.hermes/scripts
cp scripts/daily_review.py ~/.hermes/scripts/daily_review.py
hermes config set cron.wrap_response false
```

On a clean setup, choose a local review time. For example, create an `08:00` schedule:

```bash
hermes cron create "0 8 * * *" \
  --name daily-vocabulary-review \
  --script daily_review.py \
  --no-agent \
  --deliver telegram
```

This workstation already has that job scheduled for `12:00` local time. Do not create a duplicate; inspect it with:

```bash
hermes cron list
hermes cron status
```

After at least one saved word, manually run the listed job ID:

```bash
hermes cron run <job-id>
```

Expected Telegram text:

```text
What does 'obdurate' mean?
```

If a review is left unanswered, it becomes missed when the next day's review is created; there is no backlog.

## Verification and development

Run the complete suite:

```bash
uv run --extra dev pytest
```

Build the distributable package:

```bash
uv build
python3 -m zipfile -l dist/hermes_vocab-0.1.0-py3-none-any.whl
```

Operational checks:

```bash
hermes --version
hermes plugins list --plain --no-bundled
hermes tools list --platform telegram
hermes cron list
hermes gateway status
```

## Data, privacy, backup, and restore

Authoritative file:

```text
~/.local/share/hermes-vocab/vocabulary.sqlite3
```

Schema migrations run automatically when the package first initializes the database after an upgrade. Migration 002 introduced multiple senses. Migration 003 renames the word-oriented schema to entry-oriented names while preserving entry IDs, sense IDs and order, timestamps, review status, events, and answers.

The database directory and files are restricted to the current user. SQLite uses WAL, so never copy only the main file while writes may be active. Stop the gateway, then use SQLite's backup API (or copy the database and any `-wal`/`-shm` sidecars as one set). Start the gateway and run capture/review smoke checks after backup or restore.

Other local state is non-authoritative but privacy-bearing:

- Hermes conversations and memory may repeat words and answers.
- `~/.hermes/cron/jobs.json` contains scheduler metadata.
- `~/.hermes/cron/output/` may contain review words and delivery output.

Apply your normal local-history retention policy to Hermes transcripts and cron output. Back up the vocabulary SQLite database regularly; future Anki export is not a backup.

## Future extension boundaries

Anki export, weekly summaries, writing practice, and reading statistics should read from the domain/database APIs or add migrations. They should not scrape Telegram transcripts, import Hermes internals, or alter capture friction. Unrelated assistant capabilities should be separate plugins/packages rather than new branches inside `hermes_vocab`.
