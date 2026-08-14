# Hermes Vocabulary Companion

The production vocabulary companion runs in `worker/`: a Cloudflare Worker receives Telegram webhooks, a `VocabularyCompanion` Durable Object owns SQLite state and scheduling, and a cron trigger drives daily review delivery.

> **Production authority:** `worker/` is the primary implementation and the Durable Object database is the live source of truth. The Python/Hermes implementation under `src/hermes_vocab/` and `~/.local/share/hermes-vocab/vocabulary.sqlite3` are retained for migration, offline tooling, historical reference, and optional parity work; they do not automatically receive entries saved by the Worker.

See [`worker/README.md`](worker/README.md) for current deployment, operations, and migration guidance.

## Legacy local architecture

```text
configured Telegram root DM
        │
        ▼
Hermes auth + global command precedence
        │
        ├── /review ───────────────> ReviewService ────────┐
        │                                                  │
        ├── /test forward|reverse ─> TestSessionService ────┤
        │                                                  ▼
        └── non-command message ──> gateway_inbound_intercept
                                           │
                                           ├── prompt awaiting a rating
                                           ├── delivered (answerable) prompt
                                           ├── prepared-but-undelivered prompt,
                                           │   or due work with no prompt ──> interrupt
                                           ├── stored entry
                                           └── unseen entry ──> one auxiliary request
                                                                      │
                                                                      ▼
                                                              CaptureService ──> SQLite

Hermes cron (frequent tick) ──> scripts/daily_review.py ──> ReviewService ──> SQLite
          │
          └── stdout delivered to the Telegram home DM;
              at most one prompt per run, and usually nothing

Hermes outbound send ──> post_outbound_delivery ──> prompt_delivery_attempts ──> SQLite
          │
          └── only a success receipt promotes a prompt to answerable
```

Within the legacy local runtime, its SQLite database is authoritative for that independent deployment. It is not synchronized with the production Durable Object and must not be used for current production counts or scheduling claims.

### Why each component exists

- `src/hermes_vocab/capture.py`: Unicode-aware entry normalization, duplicate handling, and atomic multi-sense saves. It has no Hermes or Telegram dependency.
- `src/hermes_vocab/scheduling.py`: an embedded, dependency-free, deterministic FSRS-6 implementation. Desired retention is fixed at `0.90`, intervals are clamped to 3650 days, and there is no interval fuzzing and no parameter optimizer, so a given (schedule, rating, instant) always yields the same next interval.
- `src/hermes_vocab/review.py`: the persisted study queue. It owns card selection, prompt lifecycle transitions, answer drafts, FSRS transitions, same-entry burial, day rollover, and the immutable attempt log. It has no scheduler or transport dependency.
- `src/hermes_vocab/test_session.py`: restart-safe five-card directional test sessions and their totals, built on the same queue and the same card pool as review.
- `src/hermes_vocab/database.py`: private data-directory checks, versioned migrations, SQLite connections, WAL, and transaction policy.
- `src/hermes_vocab/models.py`: small immutable domain dataclasses and enums; no ORM.
- `src/hermes_vocab/formatting.py`: exact user-facing capture, prompt, grade-first reveal, rating, schedule, and totals text. Success text is emitted only after a committed transition.
- `src/hermes_vocab/config.py`: database path, explicit IANA timezone, optional dedicated Telegram chat, the daily new-card limit, and the local review hour.
- `src/hermes_vocab/migrations/`: append-only schema migrations owned by this package, including the v5 card/FSRS cutover and its history-replaying backfill.
- `src/hermes_vocab/hermes_plugin/`: the only Hermes-coupled layer: tools, `/review` and `/test` registration, the `gateway_inbound_intercept` router, the `post_outbound_delivery` receipt hook, contextual guidance, and focused definition/evaluation providers.
- `scripts/daily_review.py`: a thin deterministic cron entry point that ticks frequently and stays silent unless exactly one prompt should be delivered now. Empty stdout means no Telegram message.
- `tests/`: domain and real-SQLite contracts plus standalone plugin registration seams.

### Deliberate constraints

- A gateway interceptor, not only a prompt skill: prompts cannot safely own transactions, uniqueness, scheduling state, delivery receipts, or prevent general-agent consumers from claiming a dedicated message.
- A separate SQLite database, not Hermes' session database: ownership, migrations, backup, and restoration remain clear.
- One focused auxiliary definition request for an unseen entry and one focused evaluation request per ordinary forward answer. Model-generated definitions and semantic grades can be inaccurate; verify unusual or high-stakes uses.
- Scheduling is an embedded FSRS-6 implementation rather than a dependency: it is deterministic, offline, and inspectable, and each attempt records the scheduler version, parameter fingerprint, and desired retention that produced it.
- Effort ratings drive scheduling. A semantic grade only decides which ratings are legal; the rating you choose is what FSRS consumes.
- Cards, not entries, are scheduled. Review and directional tests draw from one card pool, share one daily new-card quota, and write to one immutable attempt log, so a test answer changes that card's schedule exactly like a review answer.
- A prompt is answerable only after Hermes reports a successful outbound send. Preparing text is not delivering it, and delivering it is not proving it arrived.
- All mutating study commands and every non-command study continuation are restricted to the configured, authenticated Telegram root DM. `ctx.register_command` receives only an argument string, so `/review` and `/test` re-check their `PluginCommandSource` before touching state and otherwise answer `Vocabulary study is available only in the configured Telegram root DM.`
- A no-agent cron script, not a general agent, produces review prompts. This avoids agent cost and guarantees the exact prompt text whose fingerprint the receipt hook verifies.

## Capture, review, and test behavior

### Capture

In the root Telegram DM selected by `HERMES_VOCAB_TELEGRAM_CHAT_ID`, the complete trimmed non-command message is one entry or expression:

```text
pro forma
```

Phrases are first-class entries; there is no word parser, tagging step, or context syntax in this dedicated inbox. Processing order in the interceptor is fixed:

1. A prompt awaiting an effort rating consumes the message as that rating.
2. A delivered (answerable) prompt consumes any non-hint message as its answer.
3. A prepared-but-undelivered prompt, or due work with no prompt at all, interrupts capture instead of consuming the text (see [Prompt lifecycle and failed sends](#prompt-lifecycle-and-failed-sends)).
4. A stored entry returns every SQLite sense in insertion order with `Already saved.` and makes no model request or write.
5. An unseen entry makes one focused auxiliary request for all credible senses. The complete validated set is committed in one SQLite transaction and returned with `✓ Saved.`.

Equivalent whitespace and case share one lookup key, while the first successfully captured display form is preserved. Concurrent requests converge on one complete aggregate. Exact duplicate generated `(part_of_speech, definition)` pairs collapse before persistence; malformed or partial provider output saves nothing.

Slash commands, non-configured chats, groups, and Telegram topic lanes retain normal Hermes behavior. On non-dedicated conversational Telegram surfaces, the legacy first-line entry plus optional second-line context workflow remains available through `pre_llm_call`; context syntax does not apply in the dedicated DM.

### Directional cards

Scheduling operates on cards, not entries:

- one **forward** card per entry, asking `What does '<entry>' mean?`;
- one **reverse** card per stored sense, asking `Which saved word or expression matches this definition?` followed by that exact definition.

Uniqueness is enforced in the schema (`one_forward_card_per_entry_idx`, `one_reverse_card_per_sense_idx`). Sibling cards of the same entry are scheduled independently, but after any answer every other card of that entry is buried for the rest of the local day and dropped from the current queue, so one entry cannot appear twice in a day.

Cards are created in the same transaction that saves the entry or sense, so a newly captured word is schedulable immediately. The v5 migration backfill projects cards for pre-existing entries and skips any that already have one.

### Effort ratings and FSRS

Every answer is graded semantically as `correct`, `partial`, or `incorrect`, and the grade decides which effort ratings are legal:

| Grade | Allowed ratings | Behavior |
| --- | --- | --- |
| `correct` | `Hard`, `Good`, `Easy` | The reply ends with `Choose effort: Hard or Good or Easy.` and waits for your rating. |
| `partial` | `Again`, `Hard` | The reply ends with `Choose effort: Again or Hard.` and waits for your rating. |
| `incorrect` | none | `Again` is applied automatically and the card is finalized in the same reply. |

Reverse cards are graded deterministically by exact normalized match against the saved entry, with no model call; case, repeated internal whitespace, and trailing `.`, `!`, or `?` are ignored. The exact lowercase text `show answer`, and the exact text `idk`, are deterministic surrender paths: they skip the evaluator, record `incorrect`, reveal the canonical answer, and apply `Again`.

The rating is the scheduler input. It updates the card's FSRS stability, difficulty, state, repetitions, and lapses and produces the next due instant, which the reply reports:

```text
Rated: Good
Next due: 2026-08-01 03:11 UTC
Progress: 1 of 4 complete.
```

**Retries are test-only.** In `/review`, `Again` applies the FSRS transition, reports a next due instant at least one day out, and continues without requeueing the card. In a directional `/test`, the first `Again` requeues the card once at the end and adds `Retry added at the end.`; an `Again` on that retry cannot loop again and is floored to the next local midnight.

Every finalized answer appends an immutable row to `review_attempts` recording the answer, grade, rating, before/after schedule, retrievability, and the scheduler identity that produced it. Attempt and delivery rows cannot be updated or deleted; SQLite triggers abort the attempt.

### Daily volume and backlog

- `/review` introduces up to **10 unseen cards from distinct entries** per configured local day. Unseen siblings of practiced entries are selected before cards from untouched entries, so stored senses gain review coverage before the library expands further. The quota counts distinct entries with a new card admitted by `/review` that day; an explicit directional `/test` has its own five unseen entries and does not reduce the review allowance.
- **Due work is uncapped.** Every card whose effective due instant has passed and that is not buried today is queued, ordered by due instant, then by predicted recall, then deterministically by age and ID. Due cards always precede new introductions.
- **Backlog accumulates.** Nothing is discarded, expired, or marked missed. If you skip several days, the next session simply contains every card that came due while you were away, and the 10-card introduction allowance still applies on top.
- Reviews prompt as `Review 2 of 7 · 6 due`, so the queue length and remaining due count are always visible. Directional test retries are marked `· retry`.

### `/review`

`/review` takes no arguments. Any argument returns `Usage: /review`.

It starts a new mixed-direction session, or resumes the open one, and returns the current prompt. With nothing eligible it answers `There are no eligible vocabulary cards to review.`; while a test is open it answers `Finish or exit your active test first.`

Each subsequent ordinary message in the root DM is routed by the interceptor: answer, then effort rating, then the next prompt, until the queue is empty.

### `/endstudy`

`/endstudy` ends the open review or test. Unanswered cards keep their existing due instants, no attempt is recorded, and the next `/review` or `/test <direction>` resumes that remaining work. With nothing open it answers `There is no active vocabulary study session.`

### `/test forward` and `/test reverse`

`/test` requires a direction. Bare `/test`, or any other argument, prints usage and mutates nothing:

```text
Usage: /test forward|reverse
Forward: recall each saved meaning from its word.
Reverse: recall the saved word from one exact definition.
```

A test needs exactly five unseen cards of that direction from five distinct entries. It selects the oldest unseen entries deterministically, never draws two cards from one entry, and deliberately bypasses `/review`'s daily introduction quota. Due and seen non-due cards are excluded even when their predicted recall is weak. With fewer than five unseen entries it answers, for example, `You have 3 eligible distinct reverse entries. Add or unbury 2 more to start.` and creates no session.

Prompts are numbered `Question 1 of 5`, with a tail retry shown as `Question 5 of 5 · retry`. A repeated `/test <direction>` during an active session resumes the current question instead of starting another; this is also the recovery procedure after a possibly lost Telegram delivery. The active session, current question, raw answers, drafts, and ratings survive gateway and plugin restarts. An evaluator failure consumes no attempt, reveals nothing, and leaves the same question ready for retry. After the fifth original question the session reports its totals:

```text
Forward test complete.
Results: 3 correct, 1 partial, 1 incorrect.
```

Totals count the five original questions, never their retries. Test answers are ordinary attempts: they update each card's FSRS schedule and bury that entry's siblings for the day exactly as a review answer does.

### Hints

While any prompt is answerable, the messages `hint`, `give me a hint`, `can i have a hint`, `show me an example`, or `example sentence` return `Hint: ` followed by the first stored example sentence unchanged; the hint never blanks or removes the vocabulary word when that sentence contains it. Case, repeated whitespace, and trailing `?`, `.`, or `!` are ignored. A hint makes no evaluator request, records no answer, rating, or attempt, and leaves the same question active.

### Prompt lifecycle and failed sends

A prompt moves through four distinct states, and only the last one can consume your next plain-text message:

| State | Meaning |
| --- | --- |
| **due** | A card's effective due instant has passed. No prompt row exists yet. |
| **prepared** | The exact prompt text is persisted with a stable `prompt_key`, and a delivery attempt row is recorded with status `unknown`. Not answerable. |
| **delivered** | Hermes reported a **successful** outbound send whose destination and content fingerprint match the recorded attempt. Now answerable. |
| **answerable** | The delivered prompt that the interceptor will feed your next non-hint message to. |

Hermes calls `post_outbound_delivery` with a receipt carrying state, destination, message IDs, content fingerprint, and correlation or cron-run identity. A `success` receipt promotes the prompt to delivered. A `failure` or `unknown` receipt appends a new immutable attempt row and leaves the prompt **prepared**, so it stays retryable and can never silently grade or discard your next message. Receipts are idempotent: a repeated receipt for the same identity and fingerprint changes nothing, and once a cron run reports an indeterminate outcome, later results from that run identity are ignored.

Consequently, when due work exists but no prompt is answerable — for example the computer was off, or the last send failed — an ordinary message does not get captured or graded. It is echoed back unchanged with the due question:

```text
Review due. Answer this delivered question first:

Review 1 of 3 · 3 due
What does 'obdurate' mean?

Your original message was:
xanthocroid

Complete or exit the review, then resubmit it.
```

## Current workstation status

Already completed on this machine:

- Hermes Agent `0.18.2` installed at `~/.hermes/hermes-agent` (git install method).
- The distributable vocabulary package installed into Hermes' Python environment.
- `vocabulary` entry-point plugin and Telegram toolset enabled.
- `HERMES_TIMEZONE=Asia/Kuala_Lumpur`, the default database path, and the dedicated Telegram root DM configured in `~/.hermes/.env`.
- `~/.hermes/scripts/daily_review.py` installed.
- Telegram tool progress and interim assistant messages disabled.
- `cron.wrap_response` disabled.
- The Hermes receipt patch is present in the local checkout: `hermes_cli/plugin_contracts.py` exists and `VALID_HOOKS` contains both `gateway_inbound_intercept` and `post_outbound_delivery`.

Still required by this release on this machine:

- `HERMES_VOCAB_REVIEW_HOUR` is not yet set in `~/.hermes/.env`, so the review hour falls back to `12`.
- The `daily-vocabulary-review` job (`hermes cron list`) is still on the once-daily `0 12 * * *` schedule and must be moved to a frequent tick; see [Step 5](#5-install-and-verify-the-review-ticker).
- The Hermes receipt patch is carried as uncommitted working-tree changes plus one untracked file, not as an upstream release. Re-run the [upgrade check](#hermes-companion-prerequisite) after any Hermes update.

## Setup from a clean machine

### 1. Install Hermes

Use the official installer and complete its provider setup:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes --version
hermes setup
```

The browser engine is unrelated to vocabulary. If it is slow or unwanted, install with `--skip-browser` and add it later only if another capability needs it.

### Hermes companion prerequisite

This package refuses to register mutating study surfaces against a Hermes that cannot report delivery. The Hermes checkout at `~/.hermes/hermes-agent` must provide:

- `hermes_cli.plugin_contracts` exporting `OutboundDeliveryReceipt`, `OutboundResponse`, and `PluginCommandSource`;
- `hermes_cli.plugins.VALID_HOOKS` containing both `gateway_inbound_intercept` and `post_outbound_delivery`;
- a cron runner that exports `HERMES_CRON_RUN_ID` to no-agent scripts and reports each outbound send back through `post_outbound_delivery`.

The verified reference is Hermes Agent `0.18.2` at local commit `4a29612c` plus the receipt patch, which currently lives in that checkout as modifications to `hermes_cli/plugins.py`, `gateway/run.py`, `gateway/delivery.py`, `gateway/platforms/base.py`, `cron/jobs.py`, `cron/scheduler.py`, and `tools/send_message_tool.py`, together with the new file `hermes_cli/plugin_contracts.py`.

**Fail-closed behavior.** If any of that is missing, `register()` raises `ConfigurationError` (`Installed Hermes does not expose delivery-safe plugin contracts` or `Installed Hermes does not expose the outbound receipt hook`) and no tool, command, or hook is registered. `scripts/daily_review.py` independently exits `2` with `Vocabulary cron configuration error: ...` on stderr and produces no Telegram output. Study state is never modified in either case.

**Upgrade check.** The patch is carried locally, so a Hermes upgrade or reinstall can silently drop it. Run this immediately after every `hermes` update, before trusting any prompt:

```bash
~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import sys
try:
    from hermes_cli.plugin_contracts import (
        OutboundDeliveryReceipt,
        OutboundResponse,
        PluginCommandSource,
    )
    from hermes_cli.plugins import VALID_HOOKS
except Exception as error:
    sys.exit(f"MISSING plugin contracts: {error}")
missing = {"gateway_inbound_intercept", "post_outbound_delivery"} - set(VALID_HOOKS)
if missing:
    sys.exit(f"MISSING hooks: {sorted(missing)}")
print("Hermes receipt contract OK")
PY
git -C ~/.hermes/hermes-agent status --porcelain
HERMES_PLUGINS_DEBUG=1 hermes prompt-size
```

`Hermes receipt contract OK` plus a `hermes prompt-size` run with no vocabulary configuration error means the hook survived. Anything else means the patch was lost: reapply it and re-run this check before restarting the gateway or the ticker.

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
HERMES_VOCAB_REVIEW_HOUR=8
```

Use your actual IANA timezone and allowlisted private Telegram DM ID; replace the angle-bracket placeholder rather than copying it literally. `HERMES_VOCAB_REVIEW_HOUR` is the local hour, `0`-`23`, before which the ticker stays silent unless an older overdue backlog exists; it defaults to `12`. A non-integer or out-of-range value is a configuration error, not a silent fallback. If Telegram is not configured yet, omit `HERMES_VOCAB_TELEGRAM_CHAT_ID` until Step 3. Leaving it unset disables deterministic DM routing, the `/review` and `/test` commands, and the receipt hook; tools, skill registration, and contextual guidance still load. Keep `~/.hermes/.env` permissioned to the current user; it also contains the Telegram token.

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

Before setting the chat ID, look for two vocabulary tools (`vocabulary_save_card` and `vocabulary_continue_study`), the bundled skill, `pre_llm_call`, and both `vocabulary_definition` and `vocabulary_answer_evaluation` auxiliary tasks; `/review`, `/test`, `gateway_inbound_intercept`, and `post_outbound_delivery` are intentionally absent.

### 3. Create and connect the Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`, choose the bot name and username, and copy the token.
3. Get your numeric Telegram user ID from `@userinfobot`.
4. Run:

   ```bash
   hermes gateway setup
   ```

5. Select Telegram, enter the bot token, and allowlist only your numeric user ID.
6. Add the numeric ID as `HERMES_VOCAB_TELEGRAM_CHAT_ID`, rerun the smoke commands above, and confirm `/review`, `/test`, `gateway_inbound_intercept`, and `post_outbound_delivery` now register.

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
2. Open Telegram's command menu and confirm both `/review` (“Start or resume delivery-safe vocabulary review”) and `/test forward|reverse` (“Start or resume a five-card directional vocabulary test”) appear.
3. Send an unseen phrase such as `pro forma`; confirm one numbered aggregate ends in `✓ Saved.`. Repeat the same phrase with different case and whitespace; confirm `Already saved.` and no model request.
4. Send bare `/test`. Confirm the exact three-line usage text and that nothing was mutated: `sqlite3 "$HERMES_VOCAB_DB" "SELECT COUNT(*) FROM study_sessions;"` must be unchanged.
5. Send `/test sideways`. Confirm the same usage text.
6. Send `/review`. On a database with eligible cards, confirm a prompt shaped `Review 1 of N · N due` followed by `What does '<entry>' mean?`. On an empty card pool, confirm `There are no eligible vocabulary cards to review.`
7. Send `give me a hint?`. Confirm the response is `Hint: ` followed exactly by that card's first stored example sentence, with the term visible, no grade, and no advance.
8. Answer the question with a deliberately partial answer. Confirm `Grade: Partial`, `Feedback: …`, the canonical reveal, then `Choose effort: Again or Hard.`
9. Reply `again`. Confirm `Rated: Again`, a `Next due:` at least one day out, `Progress: …`, no `Retry added at the end.`, and that the next distinct entry follows.
10. Answer the remaining review cards; confirm the failed card does not return during the same daily review.
11. Answer one question correctly and reply `easy`; confirm the next due instant moves further out than a `hard` rating on a comparable card.
12. Restart the foreground gateway mid-session — once while a prompt is unanswered and once while a prompt is awaiting its rating — and confirm the next ordinary message resumes exactly where you left off with no duplicate evaluator call.
13. With at least five eligible distinct entries, send `/test reverse`. Confirm `Question 1 of 5` and a definition-first prompt. Answer one question with the exact saved entry text in different case; confirm `Grade: Correct`. Finish the test and confirm `Reverse test complete.` with exact totals.
14. Send `/review` while that test is still open; confirm `Finish or exit your active test first.`
15. Confirm sibling burial: after answering a forward card for an entry, the same entry's reverse card does not appear again on the same local day.
16. Send `/review` from any other Telegram chat, group, or thread; confirm `Vocabulary study is available only in the configured Telegram root DM.` and that no state changed.
17. Send `/status`; confirm ordinary Hermes command behavior.

To rehearse failed-send safety without a real outage, stop the gateway while a prompt is prepared but before it is delivered — or temporarily point the bot at an unreachable network — then send an ordinary word. Confirm the word is echoed back under `Review due. Answer this delivered question first:` and that it was neither graded nor captured.

Stop the foreground process after this check, then install the persistent user service:

```bash
hermes gateway install --start-now --start-on-login
hermes gateway status
```

The local machine and gateway must be running for capture and scheduled delivery. A later move to a small VPS changes deployment and paths, not the business logic.

### 5. Install and verify the review ticker

Install the wrapper under Hermes' allowed scripts directory:

```bash
mkdir -p ~/.hermes/scripts
cp scripts/daily_review.py ~/.hermes/scripts/daily_review.py
hermes config set cron.wrap_response false
```

The script is a **frequent silent ticker**, not a morning job. Schedule it every 15 minutes:

```bash
hermes cron create "*/15 * * * *" \
  --name daily-vocabulary-review \
  --script daily_review.py \
  --no-agent \
  --deliver telegram
```

This workstation already has a `daily-vocabulary-review` job. Do not create a duplicate; move the existing one onto the frequent schedule:

```bash
hermes cron list
hermes cron edit <job-id> --schedule "*/15 * * * *"
hermes cron status
```

Each run exits `0` and prints nothing at all unless it should deliver exactly one prompt right now. It is silent when:

- a prompt is already answerable, is awaiting its effort rating, or a forward/reverse test is active;
- a prepared prompt is still in flight with no receipt yet;
- the local hour is before `HERMES_VOCAB_REVIEW_HOUR` and no card is overdue from an earlier local day;
- there is no eligible card, or the prompt could not be prepared.

It exits `2` with a stderr configuration error when Hermes' contracts, the Telegram chat ID, or `HERMES_CRON_RUN_ID` are missing, and `1` when the delivery attempt could not be recorded. In every non-zero case it prints no Telegram text.

Operational consequences, stated plainly:

- **The computer must be on.** Nothing runs while it is off or asleep. Overdue work is computed from real elapsed time the next time the ticker runs, so a weekend away produces a larger queue, not lost cards.
- **Prompts arrive one at a time.** A run delivers at most one prompt, and the next run stays silent until you answer it or the delivery attempt needs a retry. There is no burst of messages.
- **Failed or unknown sends retry.** A prompt whose receipt is not `success` stays prepared and is re-offered on a later tick or by your next ordinary message. It never consumes an answer in the meantime.
- **Unattended due work accumulates.** Nothing is discarded or marked missed. The backlog simply grows until you work through it, and the daily five-new allowance continues on top of it.
- **The ticker never marks its own delivery.** Only Hermes' `post_outbound_delivery` receipt can do that, which is exactly what makes a failed send safe.

After at least one eligible card exists and with no active test, run the listed job ID manually:

```bash
hermes cron run <job-id>
```

Expected Telegram text at or after the review hour:

```text
Review 1 of 1 · 1 due
What does 'obdurate' mean?
```

Running the job again while that prompt is unanswered delivers nothing.

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

Use the Telegram command menu and the Step 4 flow to exercise `/review` and `/test`; the generic Hermes command registry cannot prove that a command originated from the configured root DM, so the handlers re-check the source themselves. A silent ticker run is the normal case, not evidence that delivery is broken — inspect `study_prompts` and `prompt_delivery_attempts` before concluding otherwise. After any Hermes upgrade, run the [upgrade check](#hermes-companion-prerequisite) first: a dropped receipt hook shows up as a configuration error and total study silence, not as a partial failure.

## Data, privacy, backup, and restore

Authoritative file:

```text
~/.local/share/hermes-vocab/vocabulary.sqlite3
```

Schema migrations run automatically when the package first initializes the database after an upgrade, one version at a time, each in a single transaction that ends with `PRAGMA foreign_key_check`. Migration 002 introduced multiple senses. Migration 003 renamed the word-oriented schema to entry-oriented names. Migration 004 added review grade/feedback fields and the persisted `test_sessions`/`test_questions` tables. **Migration 005 (`005_spaced_review_cards.sql` plus `v005_backfill.py`) is the spaced-review cutover**: it adds `vocabulary_cards`, `study_sessions`, `study_queue`, `study_prompts`, `prompt_delivery_attempts`, `answer_drafts`, and `review_attempts`, projects forward and reverse cards from existing entries and senses, and replays trustworthy v4 history through FSRS to seed each card's schedule. The legacy `review_events`, `test_sessions`, `test_questions`, `last_reviewed`, and `review_status` columns and tables are **preserved as audit data only**; nothing reads them for scheduling any more.

### Backing up and migrating to schema v5

SQLite uses WAL, so the main file alone is not a backup. Migration is also version-coupled: the database, this package, and the patched Hermes checkout move together.

1. **Quiesce every writer.** Stop the gateway *and* every cron or ticker process, not just the gateway:

   ```bash
   hermes gateway stop
   hermes cron list                      # note the vocabulary job ID
   hermes cron pause <job-id>
   hermes cron status                    # confirm the scheduler is not running
   pgrep -fl daily_review.py             # must print nothing
   ```

2. **Back up with SQLite's backup API**, which captures committed WAL state as one consistent file. Never copy only the main file, and never copy the `-wal`/`-shm` sidecars separately:

   ```bash
   sqlite3 ~/.local/share/hermes-vocab/vocabulary.sqlite3 \
     ".backup '$HOME/vocab-pre-v5-$(date +%Y%m%d%H%M).sqlite3'"
   ```

   Record the pre-migration counts alongside the backup so the post-migration check has something to compare against:

   ```bash
   sqlite3 ~/.local/share/hermes-vocab/vocabulary.sqlite3 \
     "SELECT (SELECT COUNT(*) FROM vocabulary_entries),
             (SELECT COUNT(*) FROM vocabulary_senses),
             (SELECT COUNT(*) FROM review_events);"
   ```

3. **Rehearse on the copy first.** Point the package at the backup and let it migrate there before touching the live file:

   ```bash
   HERMES_VOCAB_DB=$HOME/vocab-pre-v5-<stamp>.sqlite3 \
   HERMES_TIMEZONE=Asia/Kuala_Lumpur \
   ~/.hermes/hermes-agent/venv/bin/python -c \
     "from hermes_vocab.database import Database; import os; Database(os.environ['HERMES_VOCAB_DB']).initialize()"
   ```

4. **Migrate the live database once.** With both compatible packages installed and all writers still stopped, initialize it exactly once — a second concurrent initializer is what turns a clean migration into a locked one:

   ```bash
   HERMES_TIMEZONE=Asia/Kuala_Lumpur \
   ~/.hermes/hermes-agent/venv/bin/python -c \
     "from hermes_vocab.config import Settings; from hermes_vocab.database import Database; Database(Settings.from_environment().database_path).initialize()"
   ```

5. **Verify before restarting anything:**

   ```bash
   sqlite3 ~/.local/share/hermes-vocab/vocabulary.sqlite3 <<'SQL'
   PRAGMA user_version;          -- must be 5
   PRAGMA integrity_check;       -- must be ok
   PRAGMA foreign_key_check;     -- must return no rows
   SELECT COUNT(*) FROM vocabulary_entries;
   SELECT COUNT(*) FROM vocabulary_senses;
   SELECT direction, COUNT(*) FROM vocabulary_cards GROUP BY direction;
   SELECT COUNT(*) FROM review_attempts;
   SELECT COUNT(*) FROM study_prompts WHERE status = 'delivered';
   SQL
   ```

   Entry and sense counts must equal the pre-migration numbers. Forward cards must equal the entry count and reverse cards the sense count. `review_attempts` must be non-zero if there was any gradable v4 history. There must be **no delivered prompt** carried over from legacy state: a migrated database starts with nothing answerable.

6. **Restart, then smoke.** Resume the job and gateway, then run the Step 4 checks:

   ```bash
   hermes cron resume <job-id>
   hermes gateway start
   hermes gateway status
   ```

**Rollback is version-coupled.** A v5 database cannot be read by an older package, and the patched Hermes checkout is part of the same cutover. Rolling back means all three together: restore the pre-migration backup file, reinstall the previous vocabulary package version, and restore the previous Hermes checkout state. Do not attempt to downgrade the schema in place, and do not run a v5 database against an unpatched Hermes — registration fails closed, so study simply stops.

The database directory and files are restricted to the current user.

Other local state is non-authoritative but privacy-bearing:

- Hermes conversations and memory may repeat entries, raw answers, model feedback, and canonical reveals. They are not a source of truth for scheduling, delivery, or study progress.
- `~/.hermes/cron/jobs.json` contains scheduler metadata.
- `~/.hermes/cron/output/` may contain review prompts and delivery output.

Apply your normal local-history retention policy to Hermes transcripts and cron output. Treat stored definitions and evaluator feedback as fallible model output, not authoritative reference material. Back up the vocabulary SQLite database regularly; future Anki export is not a backup.

## Future extension boundaries

Anki export, weekly summaries, writing practice, and reading statistics should read from the domain/database APIs or add migrations. They should not scrape Telegram transcripts, import Hermes internals, or alter capture friction. Unrelated assistant capabilities should be separate plugins/packages rather than new branches inside `hermes_vocab`.
