# Hermes Vocabulary Companion

A local-first vocabulary capture and daily-review capability for [Hermes Agent](https://hermes-agent.nousresearch.com/). Telegram and model inference stay in Hermes; vocabulary state and business rules stay in this package and its SQLite database.

Tested with Hermes Agent `0.18.2` on macOS.

## Architecture

```text
configured Telegram root DM
        │
        ▼
Hermes auth + global command precedence
        │
        ├── /test ────────────────> TestSessionService ──> SQLite
        │
        └── non-command message ──> gateway_inbound_intercept
                                           │
                                           ├── pending daily review
                                           ├── active test answer
                                           ├── stored entry
                                           └── unseen entry ──> one auxiliary request
                                                                      │
                                                                      ▼
                                                              CaptureService ──> SQLite

Hermes no-agent cron ──> scripts/daily_review.py ──> ReviewService ──> SQLite
          │
          └── exact stdout delivered to the Telegram home DM;
              active tests produce no stdout
```

SQLite is the only source of truth for saved entries, review state, semantic grades, and five-question test sessions. Hermes transcripts, memory, cron metadata, and cron output may contain copies, but none decides whether an entry exists, which review is pending, or which test question comes next.

### Why each component exists

- `src/hermes_vocab/capture.py`: Unicode-aware entry normalization, duplicate handling, and atomic multi-sense saves. It has no Hermes or Telegram dependency.
- `src/hermes_vocab/review.py`: daily selection plus pending, missed, and evaluated-answer transitions. It has no scheduler or transport dependency.
- `src/hermes_vocab/test_session.py`: restart-safe five-question sessions, answers, grades, and totals.
- `src/hermes_vocab/database.py`: private data-directory checks, migrations, SQLite connections, WAL, and transaction policy.
- `src/hermes_vocab/models.py`: small immutable domain dataclasses and enums; no ORM.
- `src/hermes_vocab/formatting.py`: exact user-facing aggregate, grade-first reveal, review, and test text. Success text is emitted only after a committed transition.
- `src/hermes_vocab/config.py`: database path, explicit IANA timezone, and optional dedicated Telegram chat resolution.
- `src/hermes_vocab/migrations/`: append-only schema migrations owned by this package.
- `src/hermes_vocab/hermes_plugin/`: the only Hermes-coupled layer: tools, `/test` registration, contextual guidance, focused definition/evaluation providers, and deterministic gateway routing.
- `scripts/daily_review.py`: a thin deterministic cron entry point. Empty stdout means no Telegram message.
- `tests/`: domain and real-SQLite contracts plus standalone plugin registration seams.

### Deliberate constraints

- A gateway interceptor, not only a prompt skill: prompts cannot safely own transactions, uniqueness, review/test lifecycle state, or prevent general-agent consumers from claiming a dedicated message.
- A separate SQLite database, not Hermes' session database: ownership, migrations, backup, and restoration remain clear.
- One focused auxiliary definition request for an unseen entry and one focused evaluation request per ordinary answer. Model-generated definitions and semantic grades can be inaccurate; verify unusual or high-stakes uses.
- No spaced-repetition algorithm, tags, or mandatory follow-up. Daily selection remains oldest never-reviewed, then least recently reviewed. A successfully completed daily review updates scheduling regardless of whether its grade is `correct`, `partial`, or `incorrect`; test answers never change ordinary review timestamps or status.
- A pending daily review owns the next non-command message before an active test; an active test owns it before capture. Slash commands retain Hermes command precedence.
- The `/test` handler is registered on Hermes' global command surface and receives only its argument string, not platform, chat, or thread metadata. The command entry itself therefore cannot enforce its source. Use it only in the configured single-user Telegram root DM; every non-command test continuation is independently restricted to that configured root DM.
- A no-agent cron job asks the morning question. This avoids general-agent cost and guarantees `What does '<entry>' mean?` exactly.

## Capture, review, and test behavior

### Capture

In the root Telegram DM selected by `HERMES_VOCAB_TELEGRAM_CHAT_ID`, the complete trimmed non-command message is one entry or expression:

```text
pro forma
```

Phrases are first-class entries; there is no word parser, tagging step, or context syntax in this dedicated inbox. Processing order is fixed:

1. A pending daily review consumes any non-hint complete message as its answer.
2. An active test consumes any non-hint complete message as the current question's answer.
3. A stored entry returns every SQLite sense in insertion order with `Already saved.` and makes no model request or write.
4. An unseen entry makes one focused auxiliary request for all credible senses. The complete validated set is committed in one SQLite transaction and returned with `✓ Saved.`.

Equivalent whitespace and case share one lookup key, while the first successfully captured display form is preserved. Concurrent requests converge on one complete aggregate. Exact duplicate generated `(part_of_speech, definition)` pairs collapse before persistence; malformed or partial provider output saves nothing.

Slash commands, non-configured chats, groups, and Telegram topic lanes retain normal Hermes behavior. On non-dedicated conversational Telegram surfaces, the legacy first-line entry plus optional second-line context workflow remains available through `pre_llm_call`; context syntax does not apply in the dedicated DM.

### Daily review and semantic grading

The morning job creates at most one entry-level event for the local date and asks `What does '<entry>' mean?`. Re-running it while that event is pending repeats the same prompt without creating another event. The next ordinary non-command root-DM answer is evaluated semantically as `correct`, `partial`, or `incorrect`. The response always presents `Grade:` and `Feedback:` before the canonical stored definition and example; multiple senses are revealed in capture order.

The exact lowercase text `show answer` is the deterministic surrender path: it skips evaluation, records `incorrect`, and reveals the canonical answer. Other ordinary answers are evaluated normally. If evaluation fails or returns invalid output, the answer is not recorded, nothing is revealed, and the same review remains pending for a retry. Only a successfully evaluated or surrendered answer completes the event and updates `last_reviewed`/`review_status`; the grade does not otherwise affect scheduling. After completion, another review run on the same local day is silent.

During a pending daily review or active test question, `hint`, `give me a hint`, `can i have a hint`, `show me an example`, or `example sentence` returns the first stored example sentence unchanged; the hint never blanks or removes the vocabulary word when that sentence contains it. Generated examples are intended to use the term. Case, repeated whitespace, and trailing `?`, `.`, or `!` are ignored. A hint makes no evaluator request, records no answer or grade, and leaves the same question active.

While a five-word test is active, the morning job is silent and creates no review event. A test completed before the morning job does not suppress that day's review.

### Five-word tests

Send parameterless `/test` after saving at least five entries. A pending daily review must be completed first.

A new test prefers entries never used in a test, then entries used least recently; ordinary daily-review priority breaks ties deterministically. Test history never changes `last_reviewed`, `review_status`, or daily-review scheduling.

The command persists all questions before replying and returns `Question 1 of 5`. Each subsequent ordinary non-command answer in the configured root DM is evaluated with the same three grades and grade-first canonical reveal; exact `show answer` records an incorrect surrender.

The active session, current question, raw answers, feedback, and category totals survive gateway/plugin restarts. If delivery may have been lost, send `/test` again to resynchronize: it resumes the first unanswered persisted question rather than starting another session. An evaluator failure consumes no question attempt, reveals nothing, and leaves that same question ready for retry. After question five, the response reports exact correct/partial/incorrect totals. Test activity does not create review events or change any entry's ordinary `last_reviewed` or `review_status`.

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

Before setting the chat ID, look for two vocabulary tools, the bundled skill, `pre_llm_call`, and both `vocabulary_definition` and `vocabulary_answer_evaluation` auxiliary tasks; `/test` and `gateway_inbound_intercept` are intentionally absent.

### 3. Create and connect the Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`, choose the bot name and username, and copy the token.
3. Get your numeric Telegram user ID from `@userinfobot`.
4. Run:

   ```bash
   hermes gateway setup
   ```

5. Select Telegram, enter the bot token, and allowlist only your numeric user ID.
6. Add the numeric ID as `HERMES_VOCAB_TELEGRAM_CHAT_ID`, rerun the smoke commands above, and confirm `/test` plus `gateway_inbound_intercept` now register.

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

In the bot's configured, allowlisted private root DM:

1. Send `/sethome`. The DM chat ID is normally the same as your user ID.
2. Open Telegram's command menu and confirm `/test` appears as “Start or resume a five-word vocabulary test.”
3. Send an unseen phrase such as `pro forma`; confirm one numbered aggregate ends in `✓ Saved.`. Repeat the same phrase with different case/whitespace; confirm `Already saved.` and no model request.
4. Using a clean database with no prior test history, save nine more entries so it contains exactly ten testable entries total. Each entry's first stored example must be a complete sentence containing that entry's displayed term.
5. Send `/test`, then send `give me a hint?`. Confirm the response consists of `Hint: ` followed exactly by the tested entry's first stored example sentence, with the tested term visible, no grade shown, and no advance to the next question.
6. Answer that same question normally. Confirm `Grade:` and `Feedback:` precede the stored definition before question 2 appears. Restart the foreground gateway, send `/test` again, and confirm it resumes the first unanswered question. This is also the recovery procedure after a possibly lost Telegram delivery.
7. Finish the first test and confirm the persisted correct/partial/incorrect totals.
8. Start a second test and confirm it uses the five entries never used in a test. Complete that test, then start a third and confirm the five least-recently-tested entries from the first test return before the more recently tested entries from the second. Test-answer continuation is supported only in this configured root DM, even though Hermes exposes the `/test` command name globally.
9. Send `/status`; confirm ordinary Hermes command behavior.

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

After at least one saved entry and with no active test, manually run the listed job ID:

```bash
hermes cron run <job-id>
```

Expected Telegram text:

```text
What does 'obdurate' mean?
```

Running the job again while that answer is pending repeats the same prompt without creating a duplicate event. After answering, another run on the same local date delivers nothing. During an active test, the job also succeeds silently and creates no review event; once the test is complete, a later run can create that day's normal review. If a review is left unanswered, it becomes missed when the next day's review is created; there is no backlog.

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

Use the Telegram command menu and the Step 4 flow for `/test`; the generic Hermes command registry cannot prove that the command originated from the configured root DM. A silent cron run is expected while a test is active, not evidence that delivery is broken. If the test finishes before the scheduled run and no review event exists for the local date, the run can create that day's normal review.

## Data, privacy, backup, and restore

Authoritative file:

```text
~/.local/share/hermes-vocab/vocabulary.sqlite3
```

Schema migrations run automatically when the package first initializes the database after an upgrade. Migration 002 introduced multiple senses. Migration 003 renamed the word-oriented schema to entry-oriented names while preserving entry IDs, sense IDs and order, timestamps, review status, events, and answers. Migration 004 adds optional review grade/feedback fields and the persisted `test_sessions`/`test_questions` tables; existing review history and scheduling fields remain unchanged, with pre-upgrade grades left null.

The database directory and files are restricted to the current user. SQLite uses WAL, so never copy only the main file while writes may be active. Stop the gateway, then use SQLite's backup API (or copy the database and any `-wal`/`-shm` sidecars as one set). Start the gateway and run capture, evaluated daily-review, and `/test` restart/resume smoke checks after backup, restore, or migration.

Other local state is non-authoritative but privacy-bearing:

- Hermes conversations and memory may repeat entries, raw answers, model feedback, and canonical reveals. They are not a source of truth for review or test progress.
- `~/.hermes/cron/jobs.json` contains scheduler metadata.
- `~/.hermes/cron/output/` may contain review entries and delivery output.

Apply your normal local-history retention policy to Hermes transcripts and cron output. Treat stored definitions and evaluator feedback as fallible model output, not authoritative reference material. Back up the vocabulary SQLite database regularly; future Anki export is not a backup.

## Future extension boundaries

Anki export, weekly summaries, writing practice, and reading statistics should read from the domain/database APIs or add migrations. They should not scrape Telegram transcripts, import Hermes internals, or alter capture friction. Unrelated assistant capabilities should be separate plugins/packages rather than new branches inside `hermes_vocab`.
