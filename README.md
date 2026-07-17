# Hermes Vocabulary Companion

A local-first vocabulary capture and daily-review capability for [Hermes Agent](https://hermes-agent.nousresearch.com/). Telegram and model inference stay in Hermes; vocabulary state and business rules stay in this package and its SQLite database.

Tested with Hermes Agent `0.18.2` on macOS.

## Architecture

```text
Telegram DM
    │
    ▼
Hermes Telegram gateway ── pre_llm_call hook ── Hermes model
                                                │
                                                ▼
                                   vocabulary_save_card tool
                                                │
                                                ▼
                                     CaptureService ── SQLite

Hermes no-agent cron ── scripts/daily_review.py ── ReviewService ── SQLite
          │
          └── exact stdout delivered to the Telegram home DM
```

SQLite is the only source of truth for saved words and review state. Hermes transcripts, memory, cron metadata, and cron output may contain copies, but none decides whether a word exists or which review is pending.

### Why each component exists

- `src/hermes_vocab/capture.py`: Unicode-aware single-word validation, normalization, duplicate handling, and atomic saves. It has no Hermes or Telegram dependency.
- `src/hermes_vocab/review.py`: daily selection and pending/missed/answered transitions. It has no scheduler or transport dependency.
- `src/hermes_vocab/database.py`: private data-directory checks, migrations, SQLite connections, WAL, and transaction policy.
- `src/hermes_vocab/models.py`: small domain dataclasses and enums; no ORM.
- `src/hermes_vocab/formatting.py`: exact user-facing capture and review text. Success text is emitted only after a committed save.
- `src/hermes_vocab/config.py`: database path and explicit IANA timezone resolution.
- `src/hermes_vocab/migrations/`: append-only schema migrations owned by this package.
- `src/hermes_vocab/hermes_plugin/`: the only Hermes-coupled layer: two tools, one Telegram routing hook, and one bundled skill.
- `scripts/daily_review.py`: a thin deterministic cron entry point. Empty stdout means no Telegram message.
- `tests/`: domain and real-SQLite contracts plus plugin registration seams.

### Deliberate constraints

- A plugin, not only a prompt skill: prompts cannot safely own transactions, uniqueness, or review lifecycle state.
- A separate SQLite database, not Hermes' session database: ownership, migrations, backup, and restoration remain clear.
- No dictionary API in V1: it would add credentials, rate limits, provider selection, and sense-disambiguation failure modes. Definitions are model-generated, so verify unusual or high-stakes words.
- No spaced repetition, tags, grading, or mandatory follow-up. The oldest never-reviewed word is chosen first, then the least recently reviewed.
- A pending review owns the next non-command Telegram message. Answer it or ask for the answer before capturing another word.
- A no-agent cron job asks the morning question. This avoids model cost and guarantees `What does '<word>' mean?` exactly.

## Capture and review behavior

Word-only capture is unchanged. Send a standalone word in the private Telegram DM:

```text
bank
```

To select a meaning with source context, put the word on the first line and the context on the remaining lines of the same message:

```text
bank
She sat on the bank and watched the river.
```

The first non-empty line must be one lexical word; the remaining text is supplied as context while preserving its internal line breaks. There is no command prefix. Consequently, any Telegram message whose first non-empty line is a standalone lexical word is intentionally routed as a vocabulary capture. A pending review takes priority, and multiline messages without a lexical first line remain ordinary conversation.

Hermes compares the intended meaning with every sense already stored for the word:

- A new word creates its first sense and ends with `✓ Saved.`.
- The same meaning, including a paraphrase, is idempotent: it creates no additional sense and ends with `Already saved with this meaning.`.
- A genuinely distinct meaning adds another sense under the same word and ends with `✓ New meaning saved.`.

Context is optional evidence for sense selection; capture does not ask a follow-up question. Daily review remains word-level: it asks one `What does '<word>' mean?` question, then reveals all stored senses in capture order after an answer or `show answer`. Multiple senses are numbered. After completion, another review run on the same local day is silent.

## Current workstation status

Already completed on this machine:

- Hermes Agent `0.18.2` installed at `~/.hermes/hermes-agent`.
- This package installed editable into Hermes' Python environment.
- `vocabulary` entry-point plugin enabled.
- Vocabulary toolset enabled for Telegram.
- `HERMES_TIMEZONE=Asia/Kuala_Lumpur` and the default database path configured in `~/.hermes/.env`.
- `~/.hermes/scripts/daily_review.py` installed.
- `cron.wrap_response` disabled.
- `daily-vocabulary-review` currently scheduled for `12:00` local time.

Provider login, Telegram credentials, the private home DM, and gateway service startup remain credential-gated. Complete Steps 3–5 below.

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
HERMES_VOCAB_DB=/Users/ryanmuenker/.local/share/hermes-vocab/vocabulary.sqlite3
```

Use your actual [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones). Keep `~/.hermes/.env` permissioned to the current user; it will also contain the Telegram token.

A plugin-load smoke check that does not call a model:

```bash
HERMES_PLUGINS_DEBUG=1 hermes prompt-size
```

Look for two registered vocabulary tools, one hook, and `vocabulary:vocabulary`.

### 3. Create and connect the Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`, choose the bot name and username, and copy the token.
3. Get your numeric Telegram user ID from `@userinfobot`.
4. Run:

   ```bash
   hermes gateway setup
   ```

5. Select Telegram, enter the bot token, and allowlist only your numeric user ID.

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

In the bot's private DM:

1. Send `/sethome`. The DM chat ID is the same as your user ID.
2. Send `obdurate`.
3. Confirm one concise card ends in `✓ Saved.`.
4. Send `obdurate` again and confirm it says `Already saved with this meaning.` without changing the first card.

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

Schema migrations run automatically when the package first initializes the database after an upgrade. Migration 002 converts a version-1 database to the word-and-senses schema while preserving existing cards and review events. Take a backup before upgrading.

The database directory and files are restricted to the current user. SQLite uses WAL, so do not copy only the main file while writes may be active. For a reliable backup or restore, stop the gateway first, then copy or replace the database and any present `-wal`/`-shm` sidecars as one set. Start the gateway again and run a capture/review smoke check.

Other local state is non-authoritative but privacy-bearing:

- Hermes conversations and memory may repeat words and answers.
- `~/.hermes/cron/jobs.json` contains scheduler metadata.
- `~/.hermes/cron/output/` may contain review words and delivery output.

Apply your normal local-history retention policy to Hermes transcripts and cron output. Back up the vocabulary SQLite database regularly; future Anki export is not a backup.

## Future extension boundaries

Anki export, weekly summaries, writing practice, and reading statistics should read from the domain/database APIs or add migrations. They should not scrape Telegram transcripts, import Hermes internals, or alter capture friction. Unrelated assistant capabilities should be separate plugins/packages rather than new branches inside `hermes_vocab`.
