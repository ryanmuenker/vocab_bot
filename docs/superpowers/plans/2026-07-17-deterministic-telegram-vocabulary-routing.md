# Deterministic Telegram Vocabulary Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the configured Telegram vocabulary root DM directly to review, SQLite entry lookup, or one focused multi-sense definition request. Treat the complete non-command message—including expressions such as `pro forma`—as the lookup text, and return exact formatter output without starting Hermes' conversational agent or showing tool activity.

**Architecture:** Add one generic post-auth, pre-free-text `gateway_inbound_intercept` hook to Hermes Agent. The vocabulary plugin claims only its configured Telegram root DM, gives pending review first priority, normalizes the complete message as an entry, serves stored entries locally, and calls Hermes' auxiliary-model client once for unseen entries. A schema-v3 migration renames the word-oriented domain to entries, and one batch capture operation validates and saves every generated sense atomically.

**Tech Stack:** Python 3.11+, asyncio, Hermes Agent plugin/gateway APIs, Hermes auxiliary LLM client, SQLite, pytest, PyYAML-backed Hermes configuration.

**Approved specification:** `docs/superpowers/specs/2026-07-17-deterministic-telegram-vocabulary-routing-design.md`

---

## Repository Boundaries

This feature spans two repositories because Hermes does not currently expose a safe post-auth gateway short-circuit.

### Hermes Agent checkout

Root: `/Users/ryanmuenker/.hermes/hermes-agent`

- Modify `hermes_cli/plugins.py`: public hook name, immutable handled-response type, and ordered async first-match dispatch.
- Modify `gateway/run.py`: preserve original slash-command state and invoke the scalar-only hook after authorization but before every non-command free-text consumer.
- Modify `tests/hermes_cli/test_plugins.py`: ordering, short-circuiting, exception isolation, and response validation.
- Modify `tests/gateway/test_telegram_topic_mode.py`: authorization, active-session, pending-prompt, command, scalar-payload, and no-agent assertions.

Create a local branch named `feat/gateway-inbound-intercept` from the checkout's current commit before editing. The installed checkout currently has a clean `main` that is one commit ahead and one behind `origin/main`; do not reset, rebase, or discard that local commit as part of this feature.

### Vocabulary package

Root: `/Users/ryanmuenker/Desktop/hermes`

- Create `src/hermes_vocab/migrations/003_entry_terms.sql` and modify the domain/storage/review/formatting modules for the entry-oriented schema.
- Modify plugin schemas, tools, hooks, and skill instructions to complete the `word` → `entry` cutover without aliases.
- Add one atomic multi-sense batch capture operation beneath both routing and future callers.
- Modify `src/hermes_vocab/config.py`: optional dedicated Telegram root-DM chat ID.
- Create `src/hermes_vocab/hermes_plugin/definition.py`: one auxiliary LLM call and strict multi-sense response validation.
- Create `src/hermes_vocab/hermes_plugin/gateway.py`: deterministic review/full-message lookup routing.
- Modify `src/hermes_vocab/hermes_plugin/__init__.py`: register the auxiliary task and inbound interceptor while retaining the non-dedicated contextual `pre_llm_call` route.
- Update focused migration, capture, review, formatting, provider, router, and plugin integration tests.
- Modify `README.md`: entry/phrase semantics, migration, configuration, latency, and Hermes-core prerequisite.

No new third-party dependency is required. Vocabulary-package coroutine tests use `asyncio.run()`; the Hermes checkout retains its existing async pytest support.

---

### Task 1: Add the async Hermes plugin-hook contract

**Repository:** `/Users/ryanmuenker/.hermes/hermes-agent`

**Files:**
- Modify: `hermes_cli/plugins.py:135-173, 1888-1927, 2025-2076`
- Modify: `tests/hermes_cli/test_plugins.py`

- [ ] **Step 1: Create the Hermes feature branch**

Run:

```bash
git switch -c feat/gateway-inbound-intercept
```

Expected: the new branch points at the current local `main` commit; the working tree remains clean.

- [ ] **Step 2: Write failing response-contract and async-hook tests**

Add focused tests to `tests/hermes_cli/test_plugins.py` using the existing `PluginManager` fixtures and reset helpers:

```python
import asyncio

import pytest

from hermes_cli.plugins import GatewayInterceptResponse, PluginManager


def test_gateway_intercept_response_rejects_blank_text() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GatewayInterceptResponse("   ")


@pytest.mark.asyncio
async def test_invoke_hook_until_type_awaits_in_order_and_stops() -> None:
    manager = PluginManager()
    calls: list[str] = []

    def first(**kwargs):
        calls.append(f"first:{kwargs['user_message']}")
        return None

    async def second(**kwargs):
        await asyncio.sleep(0)
        calls.append(f"second:{kwargs['user_message']}")
        return GatewayInterceptResponse("handled")

    def must_not_run(**kwargs):
        raise AssertionError("callbacks after a handled response must not run")

    manager._hooks["gateway_inbound_intercept"] = [
        first,
        second,
        must_not_run,
    ]

    result = await manager.invoke_hook_until_type_async(
        "gateway_inbound_intercept",
        GatewayInterceptResponse,
        user_message="perfidy",
    )

    assert calls == ["first:perfidy", "second:perfidy"]
    assert result == GatewayInterceptResponse("handled")


@pytest.mark.asyncio
async def test_invoke_hook_until_type_isolates_callback_exceptions() -> None:
    manager = PluginManager()

    async def broken(**kwargs):
        raise RuntimeError("broken plugin")

    def healthy(**kwargs):
        return GatewayInterceptResponse("healthy")

    manager._hooks["gateway_inbound_intercept"] = [broken, healthy]

    assert await manager.invoke_hook_until_type_async(
        "gateway_inbound_intercept",
        GatewayInterceptResponse,
    ) == GatewayInterceptResponse("healthy")
```

Add one callback test whose signature lists the documented gateway scalar fields explicitly and does not accept `**kwargs`. Invoke it with that exact payload and assert it handles successfully. This prevents the helper from injecting observer telemetry or any undeclared field.

Also assert `"gateway_inbound_intercept" in VALID_HOOKS` in the existing valid-hook test.

- [ ] **Step 3: Run the focused tests and confirm red**

Run:

```bash
python -m pytest tests/hermes_cli/test_plugins.py -k 'gateway_intercept or invoke_hook_until_type' -q
```

Expected: collection or assertion failures because `GatewayInterceptResponse`, `invoke_hook_until_type_async`, and the hook registry entry do not exist.

- [ ] **Step 4: Implement the minimal public contract**

In `hermes_cli/plugins.py`, add the hook to `VALID_HOOKS` with comments documenting that it runs after gateway authorization but before any non-command free-text consumer, session, or model work.

Add the immutable result type beside other public plugin contracts:

```python
@dataclass(frozen=True, slots=True)
class GatewayInterceptResponse:
    """Exact text returned by a plugin that handled an inbound gateway message."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Gateway intercept response text must be non-empty")
```

Add an async first-match method without changing the semantics of the existing synchronous `invoke_hook()`:

```python
async def invoke_hook_until_type_async(
    self,
    hook_name: str,
    result_type: type[Any],
    **kwargs: Any,
) -> Any | None:
    """Return the first callback result of result_type in registration order."""
    for callback in self._hooks.get(hook_name, []):
        try:
            result = callback(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, result_type):
                return result
        except Exception as exc:
            logger.warning(
                "Hook '%s' callback %s raised: %s",
                hook_name,
                getattr(callback, "__name__", repr(callback)),
                exc,
            )
    return None
```

Add the module-level wrapper beside `invoke_hook()`:

```python
async def invoke_hook_until_type_async(
    hook_name: str,
    result_type: type[Any],
    **kwargs: Any,
) -> Any | None:
    """Return the first matching result from sync or async hook callbacks."""
    return await get_plugin_manager().invoke_hook_until_type_async(
        hook_name,
        result_type,
        **kwargs,
    )
```

Unlike general observation hooks, this first-match helper forwards exactly the kwargs supplied by the gateway. It must not inject `telemetry_schema_version` or any other field; the gateway interceptor contract is scalar-only and explicit-parameter callbacks must remain valid.

Do not make synchronous hooks silently execute coroutines. The first matching handled response ends dispatch immediately, so later plugins cannot add side effects or latency after the message has been claimed.

- [ ] **Step 5: Run the focused tests and confirm green**

Run:

```bash
python -m pytest tests/hermes_cli/test_plugins.py -k 'gateway_intercept or invoke_hook_until_type' -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the Hermes hook contract**

Stage only `hermes_cli/plugins.py` and `tests/hermes_cli/test_plugins.py`. Commit with the repository-required decision trailers:

```text
Let plugins answer authenticated gateway messages without an agent turn

Constraint: Existing synchronous hook behavior must remain unchanged
Rejected: Reuse pre_gateway_dispatch | it runs before authorization
Confidence: high
Scope-risk: moderate
Directive: Keep gateway_inbound_intercept transport-neutral and response-only
Tested: python -m pytest tests/hermes_cli/test_plugins.py -k 'gateway_intercept or invoke_hook_until_type' -q
```

---

### Task 2: Intercept authenticated text before every free-text consumer

**Repository:** `/Users/ryanmuenker/.hermes/hermes-agent`

**Files:**
- Modify: `gateway/run.py:9004-9151`
- Modify: `tests/gateway/test_telegram_topic_mode.py`

- [ ] **Step 1: Write failing gateway-ordering tests**

Add `GatewayInterceptResponse` to the imports in `tests/gateway/test_telegram_topic_mode.py`. Use that file's complete `_make_runner()` and `_make_event()` fixtures; do not create a second partial runner fixture.

Add focused tests for these contracts:

```python
@pytest.mark.asyncio
async def test_gateway_interceptor_handles_before_active_session(monkeypatch):
    response = GatewayInterceptResponse("handled")
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook_until_type_async",
        invoke,
    )
    runner = _make_runner()
    quick_key = runner._session_key_for_source(_make_source())
    runner._running_agents[quick_key] = object()
    runner._claim_active_session_slot = MagicMock(
        side_effect=AssertionError("handled text reached session claim")
    )

    assert await runner._handle_message(_make_event("pro forma")) == "handled"
    runner._claim_active_session_slot.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_interceptor_handles_before_pending_update(monkeypatch):
    invoke = AsyncMock(return_value=GatewayInterceptResponse("handled"))
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook_until_type_async",
        invoke,
    )
    runner = _make_runner()
    quick_key = runner._session_key_for_source(_make_source())
    runner._update_prompt_pending = {quick_key: object()}

    assert await runner._handle_message(_make_event("pro forma")) == "handled"


@pytest.mark.asyncio
async def test_unauthorized_message_never_reaches_interceptor(monkeypatch):
    invoke = AsyncMock()
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook_until_type_async",
        invoke,
    )
    runner = _make_runner()
    runner._is_user_authorized = MagicMock(return_value=False)
    runner._get_unauthorized_dm_behavior = MagicMock(return_value="ignore")

    assert await runner._handle_message(_make_event("pro forma")) is None
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_slash_command_bypasses_after_rewrite(monkeypatch):
    invoke = AsyncMock(return_value=GatewayInterceptResponse("wrong"))
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook_until_type_async",
        invoke,
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kwargs: (
            [{"action": "rewrite", "text": "pro forma"}]
            if name == "pre_gateway_dispatch"
            else []
        ),
    )
    runner = _make_runner()
    runner._telegram_topic_mode_enabled = lambda source: False
    runner._handle_message_with_agent = AsyncMock(return_value="agent response")

    assert await runner._handle_message(_make_event("/steer pro forma")) == (
        "agent response"
    )
    invoke.assert_not_awaited()
```

Add equivalent handled-response tests for clarification and slash-confirmation consumers. Patch `tools.clarify_gateway.get_pending_for_session` and `tools.slash_confirm.get_pending` with mocks that raise `AssertionError` if called; the inbound interceptor must return before either lookup.

Add one decline test returning `None` and asserting the normal agent path runs. Add one payload test with all reply fields populated and assert the async hook receives only:

```python
{
    "platform",
    "sender_id",
    "chat_id",
    "chat_type",
    "thread_id",
    "user_message",
    "reply_to_message_id",
    "reply_to_text",
    "reply_to_author_id",
    "reply_to_author_name",
    "reply_to_is_own_message",
}
```

The payload assertion must explicitly reject `event`, `gateway`, `session_store`, `raw_message`, `metadata`, and media paths.

- [ ] **Step 2: Run the focused tests and confirm red**

```bash
python -m pytest \
  tests/gateway/test_telegram_topic_mode.py \
  -k 'gateway_interceptor or unauthorized_message or original_slash' \
  -q
```

Expected: failures because authenticated non-command text is consumed by pending/running-session branches before any inbound interceptor exists.

- [ ] **Step 3: Preserve slash-command identity before rewrites**

At the beginning of `GatewayRunner._handle_message()`, immediately after `source = event.source`, record:

```python
        was_slash_command = event.get_command() is not None
```

After `pre_gateway_dispatch` finishes and may replace `event`, merge the rewritten state without losing the original:

```python
        was_slash_command = (
            was_slash_command or event.get_command() is not None
        )
```

Do not recompute this flag after command handlers rewrite a command into plain payload text.

- [ ] **Step 4: Insert the post-auth, pre-free-text interceptor**

Insert the block immediately after the authorization branch returns for unauthorized users (`gateway/run.py:9149` in the current checkout) and before `_update_prompt_pending`, clarification, confirmation, running-agent, topic-lobby, or session logic:

```python
        if not is_internal and not was_slash_command:
            try:
                from hermes_cli.plugins import (
                    GatewayInterceptResponse,
                    invoke_hook_until_type_async,
                )

                intercept_response = await invoke_hook_until_type_async(
                    "gateway_inbound_intercept",
                    GatewayInterceptResponse,
                    platform=(
                        source.platform.value if source.platform else None
                    ),
                    sender_id=source.user_id,
                    chat_id=source.chat_id,
                    chat_type=source.chat_type,
                    thread_id=source.thread_id,
                    user_message=event.text,
                    reply_to_message_id=event.reply_to_message_id,
                    reply_to_text=event.reply_to_text,
                    reply_to_author_id=event.reply_to_author_id,
                    reply_to_author_name=event.reply_to_author_name,
                    reply_to_is_own_message=event.reply_to_is_own_message,
                )
            except Exception as exc:
                logger.warning(
                    "gateway_inbound_intercept invocation failed: %s",
                    exc,
                )
                intercept_response = None

            if intercept_response is not None:
                return intercept_response.text
```

This generic hook runs for every authenticated non-command message. Plugins decide scope from scalar fields. Do not pass the mutable event or gateway internals.

- [ ] **Step 5: Run gateway and neighboring regressions**

```bash
python -m pytest \
  tests/gateway/test_telegram_topic_mode.py \
  tests/gateway/test_pre_gateway_dispatch.py \
  tests/gateway/test_unknown_command.py \
  tests/gateway/test_telegram_slash_confirm.py \
  -q
```

Expected: interception ordering tests pass; authorization, pre-dispatch rewrites, slash commands, topic mode, and normal decline behavior remain green.

- [ ] **Step 6: Commit the Hermes gateway integration**

Stage only `gateway/run.py` and `tests/gateway/test_telegram_topic_mode.py`. Commit:

```text
Keep plugin-owned messages out of conversational consumers

Constraint: Authorization and original slash-command identity retain precedence
Rejected: Insert after command handling | active and pending flows consume text earlier
Confidence: high
Scope-risk: moderate
Directive: Keep gateway_inbound_intercept scalar-only and before all free-text consumers
Tested: focused gateway, auth, command, topic, and pre-dispatch tests
```

### Task 3: Migrate the vocabulary domain from words to entries

**Repository:** `/Users/ryanmuenker/Desktop/hermes`

**Files:**
- Create: `src/hermes_vocab/migrations/003_entry_terms.sql`
- Modify: `src/hermes_vocab/models.py`
- Modify: `src/hermes_vocab/database.py`
- Modify: `src/hermes_vocab/capture.py`
- Modify: `src/hermes_vocab/review.py`
- Modify: `src/hermes_vocab/formatting.py`
- Modify: `src/hermes_vocab/hermes_plugin/hooks.py`
- Modify: `src/hermes_vocab/hermes_plugin/tools.py`
- Modify: `src/hermes_vocab/hermes_plugin/schemas.py`
- Modify: `src/hermes_vocab/hermes_plugin/skills/vocabulary/SKILL.md`
- Modify: `tests/integration/test_database.py`
- Modify: `tests/unit/test_capture.py`
- Verify: `tests/unit/test_capture_parser.py`
- Modify: `tests/unit/test_review.py`
- Modify: `tests/unit/test_formatting.py`
- Modify: `tests/unit/test_daily_review.py`
- Modify: `tests/integration/test_hermes_plugin.py`

- [ ] **Step 1: Map exported-symbol call sites before renaming**

Use LSP references for every exported type or method being renamed:

```text
CaptureOperation
CaptureRequest
CaptureCommand
VocabularyWord
VocabularySense.word_id
ReviewEvent.word_id
ReviewPromptResult.word
ReviewCompletionResult.word
CaptureService.get_word
```

Apply symbol-aware renames where the Python server supports them. Do not use global text replacement. Old migration files `001_initial.sql` and `002_multi_sense.sql` are immutable historical artifacts and intentionally retain their old names.

- [ ] **Step 2: Write failing v2-to-v3 migration tests**

Add `create_v2_database(path)` beside `create_v1_database()`. Apply migrations 001 and 002, then seed:

- entry ID 7, display text `Pro Forma`, normalized text `pro forma`
- two ordered senses with stable IDs and different `source_context`
- one answered review event referencing entry 7

Add these tests:

```python
def test_v2_migration_preserves_entries_senses_and_reviews(tmp_path):
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v2_database(path)

    Database(path).initialize()

    with Database(path).connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"vocabulary_entries", "vocabulary_senses", "review_events"} <= tables
        assert "vocabulary_words" not in tables
        assert tuple(
            connection.execute(
                """
                SELECT id, display_text, normalized_text,
                       date_added, last_reviewed, review_status
                FROM vocabulary_entries
                """
            ).fetchone()
        ) == (
            7,
            "Pro Forma",
            "pro forma",
            "2026-01-01T00:00:00Z",
            "2026-02-01T00:00:00Z",
            "reviewed",
        )
```

Also assert:

- sense IDs, ordering, definitions, examples, source contexts, and timestamps are unchanged;
- `vocabulary_senses.entry_id` and `review_events.entry_id` both equal 7;
- review event ID/status/answer timestamps/text are unchanged;
- fresh initialization lands directly on version 3;
- concurrent fresh and v2 initialization are idempotent at version 3;
- skipping from version 1 directly to migration 3 is rejected;
- migration 3 does not run a second time after another initializer commits it.

- [ ] **Step 3: Run migration tests and confirm red**

```bash
python -m pytest tests/integration/test_database.py -q
```

Expected: assertions fail because the runtime target is still version 2 and schema 003 does not exist.

- [ ] **Step 4: Add the atomic schema-v3 migration**

Create `003_entry_terms.sql`:

```sql
ALTER TABLE vocabulary_words RENAME TO vocabulary_entries;
ALTER TABLE vocabulary_entries RENAME COLUMN word TO display_text;
ALTER TABLE vocabulary_entries
    RENAME COLUMN normalized_word TO normalized_text;

ALTER TABLE vocabulary_senses RENAME COLUMN word_id TO entry_id;
ALTER TABLE review_events RENAME COLUMN word_id TO entry_id;

DROP INDEX vocabulary_senses_word_order_idx;
DROP INDEX review_events_word_id_idx;
DROP INDEX vocabulary_review_order_idx;

CREATE INDEX vocabulary_senses_entry_order_idx
    ON vocabulary_senses(entry_id, date_added, id);
CREATE INDEX review_events_entry_id_idx ON review_events(entry_id);
CREATE INDEX vocabulary_review_order_idx
    ON vocabulary_entries(last_reviewed, date_added, id);

PRAGMA user_version = 3;
```

Before locking this SQL, run it against a seeded v2 fixture and verify SQLite accepts `ALTER TABLE ... RENAME COLUMN` while foreign keys are enabled. If index names are automatically rewritten by SQLite before the explicit `DROP INDEX`, inspect `sqlite_master` and adjust only the index-drop ordering; preserve the final v3 schema above.

Add `3: "003_entry_terms.sql"` to `Database._MIGRATIONS`. Keep the existing `BEGIN IMMEDIATE`, in-lock `PRAGMA user_version` recheck, exact previous-version guard, foreign-key check, rollback, and commit behavior unchanged.

- [ ] **Step 5: Rename the Python domain cleanly**

Use these final contracts:

```python
class CaptureOperation(StrEnum):
    NEW_ENTRY = "new_entry"
    NEW_SENSE = "new_sense"
    EXISTING_SENSE = "existing_sense"


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    display_text: str
    context: str | None


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    display_text: str
    operation: CaptureOperation
    card: SenseCard | None = None
    source_context: str | None = None
    matching_sense_id: int | None = None


@dataclass(frozen=True, slots=True)
class VocabularySense:
    id: int
    entry_id: int
    ...


@dataclass(frozen=True, slots=True)
class VocabularyEntry:
    id: int
    display_text: str
    normalized_text: str
    ...
    senses: tuple[VocabularySense, ...]
```

Rename result/event fields to `entry`, service methods to `get_entry()`, SQL helpers to `_entry_from_rows()`, and SQL aliases/parameters to entry terminology. Update review selection and event completion to read `vocabulary_entries` and `entry_id`.

Do not leave `VocabularyWord`, `get_word`, `word_id`, `normalized_word`, `NEW_WORD`, aliases, deprecated exports, or dual-schema branches in runtime source. Existing user-visible review copy may continue to say “word”; that is product language, not a domain identifier.

- [ ] **Step 6: Normalize complete entries rather than lexical words**

Replace lexical-character validation with a shared normalizer that:

1. applies Unicode NFKC to the complete message;
2. strips only leading and trailing whitespace to produce `display_text`;
3. rejects an empty `display_text` or one longer than 500 Unicode code points;
4. collapses each internal whitespace run in a copy to one ASCII space;
5. `casefold()`s that copy to produce `normalized_text`;
6. preserves the first successfully captured `display_text`, including its internal spacing, for presentation.

The dedicated route and capture service share this result contract:

```python
class EntryTextStatus(StrEnum):
    VALID = "valid"
    EMPTY = "empty"
    TOO_LONG = "too_long"


@dataclass(frozen=True, slots=True)
class NormalizedEntryText:
    status: EntryTextStatus
    display_text: str | None = None
    normalized_text: str | None = None
```

Preserve the existing two-line contextual parser for non-dedicated Hermes surfaces, but return `CaptureRequest(display_text=..., context=...)`.

Required cases:

```python
assert normalize_entry_text("  Pro   Forma  ") == NormalizedEntryText(
    EntryTextStatus.VALID,
    display_text="Pro   Forma",
    normalized_text="pro forma",
)
assert normalize_entry_text("   ").status is EntryTextStatus.EMPTY
assert normalize_entry_text("x" * 501).status is EntryTextStatus.TOO_LONG
```

- [ ] **Step 7: Update storage, formatters, plugin schemas, and all callers**

Complete the cutover in one change:

- SQL uses `vocabulary_entries(display_text, normalized_text, ...)`.
- Sense ordering remains `ORDER BY date_added, id`; never order by wall-clock time alone.
- `format_capture()`, `format_daily_review()`, `format_review_completion()`, and read-only aggregate formatting accept `VocabularyEntry`.
- Tool schema changes `word` → `display_text` and operation `new_word` → `new_entry`.
- `ToolHandlers.save_card()` maps only the new schema.
- `VocabularyHook` instructions and bundled `SKILL.md` describe entries/expressions while preserving contextual second-line behavior outside the dedicated DM.
- Tests and README examples that inspect schema identifiers use v3 names.

This is a clean cutover. Do not add compatibility fields because the package and plugin are released together.

- [ ] **Step 8: Run focused domain regressions**

```bash
python -m pytest \
  tests/integration/test_database.py \
  tests/unit/test_capture.py \
  tests/unit/test_capture_parser.py \
  tests/unit/test_review.py \
  tests/unit/test_formatting.py \
  tests/unit/test_daily_review.py \
  tests/integration/test_hermes_plugin.py \
  -q
```

Expected: v1 and v2 fixtures migrate to v3 without loss; entry expressions normalize and look up correctly; review behavior and ordered multi-sense formatting remain green.

- [ ] **Step 9: Commit the entry-domain migration**

```text
Model captured expressions without word-only schema constraints

Constraint: Existing SQLite IDs, senses, and review history must survive intact
Rejected: Keep word aliases | parallel terminology would prolong schema ambiguity
Confidence: high
Scope-risk: broad
Directive: Keep migrations immutable and use entry_id for all runtime foreign keys
Tested: migration, capture, review, formatting, and plugin integration tests
```

### Task 4: Add one atomic multi-sense capture operation

**Repository:** `/Users/ryanmuenker/Desktop/hermes`

**Files:**
- Modify: `src/hermes_vocab/models.py`
- Modify: `src/hermes_vocab/capture.py`
- Modify: `tests/unit/test_capture.py`
- Modify: `tests/integration/test_database.py`

- [ ] **Step 1: Write failing batch-capture tests**

Add tests that use a real temporary SQLite database and a fixed clock:

```python
def test_capture_entry_saves_all_senses_in_one_ordered_aggregate(service):
    result = service.capture_entry(
        "Pro Forma",
        (
            SenseCard(
                part_of_speech="adjective",
                definition="Provided as a matter of form.",
                example_sentence="The board issued a pro forma approval.",
            ),
            SenseCard(
                part_of_speech="noun",
                definition="A projected financial statement.",
                example_sentence="The analyst prepared a pro forma.",
            ),
        ),
    )

    assert result.status is CaptureStatus.SAVED
    assert result.entry is not None
    assert result.entry.display_text == "Pro Forma"
    assert [sense.part_of_speech for sense in result.entry.senses] == [
        "adjective",
        "noun",
    ]
```

Add separate tests for:

- whitespace/case-equivalent `PRO   FORMA` returns `ALREADY_EXISTS` and performs no insert;
- the response for an existing entry returns every stored sense in insertion-ID order;
- empty cards are `INVALID`;
- more than 20 cards are `INVALID`;
- duplicate cards presented directly to the batch API are `INVALID`; Task 5's provider parser removes exact generated duplicates before calling this boundary;
- blank/oversized part of speech, definition, or example is `INVALID`;
- malformed later card rolls back both the entry and all earlier senses;
- an injected `sqlite3.Error` on the second sense insert returns `STORAGE_ERROR` and leaves no partial entry;
- two concurrent fresh captures converge on one entry and one complete sense set;
- if the competing committed aggregate differs, the loser returns the committed aggregate rather than merging, replacing, or partially duplicating senses.

For concurrency, coordinate two service calls with a barrier around transaction start; do not assert which caller wins.

- [ ] **Step 2: Run the focused tests and confirm red**

```bash
python -m pytest \
  tests/unit/test_capture.py \
  tests/integration/test_database.py \
  -k 'capture_entry or batch or concurrent' \
  -q
```

Expected: failures because only one-card operation-oriented capture exists.

- [ ] **Step 3: Define the batch result contract**

Add:

```python
@dataclass(frozen=True, slots=True)
class EntryCaptureResult:
    status: CaptureStatus
    entry: VocabularyEntry | None = None
```

The method boundary is:

```python
def capture_entry(
    self,
    display_text: str,
    cards: Sequence[SenseCard],
) -> EntryCaptureResult:
    ...
```

Copy the incoming sequence to a tuple once at the boundary. Do not expose mutable collections in result models.

- [ ] **Step 4: Validate the complete request before opening a transaction**

Use the shared entry normalizer from Task 3. Validate:

- 1–20 cards;
- each `part_of_speech`, `definition`, and `example_sentence` is a string after provider parsing;
- trimmed, non-empty fields;
- existing field-length constants;
- no duplicate normalized `(part_of_speech, definition)` pair in the same batch.

Return `INVALID` without touching SQLite when any card fails. The batch boundary never silently repairs a caller's command. The focused provider parser is the one deliberate exception: it collapses exact generated duplicates before constructing this already-valid batch.

- [ ] **Step 5: Save the aggregate in one immediate transaction**

Implement:

```python
with self._database.connect() as connection:
    connection.execute("BEGIN IMMEDIATE")
    existing = self._get_entry_with_connection(
        connection,
        normalized_text,
    )
    if existing is not None:
        connection.rollback()
        return EntryCaptureResult(
            CaptureStatus.ALREADY_EXISTS,
            existing,
        )

    cursor = connection.execute(
        """
        INSERT INTO vocabulary_entries (
            display_text, normalized_text, date_added
        ) VALUES (?, ?, ?)
        """,
        (display_text, normalized_text, timestamp),
    )
    entry_id = int(cursor.lastrowid)
    for card in cards:
        connection.execute(
            """
            INSERT INTO vocabulary_senses (
                entry_id, definition, part_of_speech,
                example_sentence, source_context, date_added
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (
                entry_id,
                card.definition.strip(),
                card.part_of_speech.strip(),
                card.example_sentence.strip(),
                timestamp,
            ),
        )
    entry = self._get_entry_by_id_with_connection(connection, entry_id)
    if entry is None or len(entry.senses) != len(cards):
        raise sqlite3.DatabaseError("incomplete capture aggregate")
    connection.commit()
    return EntryCaptureResult(CaptureStatus.SAVED, entry)
```

Read and validate the aggregate inside the same transaction before commit, then return that immutable value only after `commit()` succeeds. This avoids a post-commit read failure that could report an error even though data was saved. Every sense shares the batch timestamp, so deterministic order depends on `id`, not wall-clock resolution.

Catch `sqlite3.Error`, roll back, and return `STORAGE_ERROR`. Preserve the existing one-corrected-retry `CONFLICT` behavior for the non-dedicated operation-oriented `capture()` method; the dedicated batch method never asks the model to select an operation.

- [ ] **Step 6: Handle concurrent convergence without partial results**

`BEGIN IMMEDIATE` serializes fresh writers. The second batch caller must recheck after obtaining the write lock and return `ALREADY_EXISTS` with the committed aggregate. At this storage boundary it must not:

- append its generated senses;
- overwrite the winner;
- expose an empty or partially inserted entry.

Task 6's router single flight prevents duplicate process-local provider requests; the capture service independently guarantees convergence against other processes.

Retain the database's 5-second busy timeout. A lock timeout is `STORAGE_ERROR` and uses the existing user-safe retry guidance.

- [ ] **Step 7: Run capture and migration regressions**

```bash
python -m pytest \
  tests/unit/test_capture.py \
  tests/integration/test_database.py \
  -q
```

Expected: atomicity, ordering, duplicate detection, storage-error translation, migration, and concurrent convergence tests pass.

- [ ] **Step 8: Commit the atomic aggregate**

```text
Keep multi-sense captures whole under failure and concurrency

Constraint: Every returned sense must be persisted in one transaction
Rejected: Loop over single-sense capture | partial batches and operation ambiguity leak through
Confidence: high
Scope-risk: moderate
Directive: Validate all cards before BEGIN IMMEDIATE and preserve insertion-ID ordering
Tested: batch capture, rollback, concurrency, and migration tests
```

### Task 5: Implement one focused multi-sense definition request

**Repository:** `/Users/ryanmuenker/Desktop/hermes`

**Files:**
- Create: `src/hermes_vocab/hermes_plugin/definition.py`
- Create: `tests/unit/test_definition.py`

- [ ] **Step 1: Write failing parser and provider tests**

Test a `DefinitionProvider` with an injected async callable; unit tests must never import Hermes or contact a provider.

Cover:

```python
def test_parse_definition_response_returns_ordered_senses():
    response = json.dumps(
        {
            "senses": [
                {
                    "part_of_speech": "adjective",
                    "definition": "Provided as a matter of form.",
                    "example_sentence": "The vote was pro forma.",
                },
                {
                    "part_of_speech": "noun",
                    "definition": "A projected financial statement.",
                    "example_sentence": "She prepared a pro forma.",
                },
            ]
        }
    )

    result = parse_definition_response(response)

    assert result.status is DefinitionStatus.FOUND
    assert [card.part_of_speech for card in result.cards] == [
        "adjective",
        "noun",
    ]
```

Parameterized invalid responses:

- non-JSON;
- a top-level list;
- missing or extra top-level keys;
- non-list `senses`;
- more than 20 senses;
- a sense missing or adding a field;
- non-string, blank, or oversized fields;
- markdown fences or prose around otherwise valid JSON;
- empty `senses`, which is not a defined result;
- `{"status": "not_found", "senses": []}`, because not-found must contain no senses.

Add a positive test with repeated normalized `(part_of_speech, definition)` pairs and assert the parser retains the first card, removes later duplicates, preserves remaining model order, and returns `FOUND`. `{"status": "not_found"}` returns `NOT_FOUND`. Provider exceptions and empty content are `PROVIDER_ERROR` and `INVALID_RESPONSE`, respectively. Assert each `define()` invocation calls the injected callable exactly once with `tools=[]`.

- [ ] **Step 2: Run the tests and confirm red**

```bash
python -m pytest tests/unit/test_definition.py -q
```

Expected: import failure because the provider module does not exist.

- [ ] **Step 3: Define strict result models**

Inside `definition.py`:

```python
MAX_SENSES = 20


class DefinitionStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class DefinitionResult:
    status: DefinitionStatus
    cards: tuple[SenseCard, ...] = ()
```

`parse_definition_response(text)` must use `json.loads(text)` directly. Accept exactly one of these disjoint schemas:

Defined:

```json
{
  "senses": [
    {
      "part_of_speech": "string",
      "definition": "string",
      "example_sentence": "string"
    }
  ]
}
```

Not found:

```json
{"status": "not_found"}
```

The defined array must contain 1–20 raw senses. Validate every item first, then remove later duplicate normalized `(part_of_speech, definition)` pairs while preserving model order. Do not strip markdown fences, extract substrings, coerce values, invent examples, or accept partial arrays. Reuse capture field limits and duplicate-key normalization so parsing and persistence agree.

- [ ] **Step 4: Build the bounded prompt**

`DefinitionProvider.define(display_text)` accepts only the NFKC-normalized, edge-trimmed display string. Send:

```python
messages = [
    {
        "role": "system",
        "content": (
            "You are a focused English dictionary enrichment service. "
            "Return JSON only. For a defined entry, return exactly one "
            "top-level key, senses, containing 1 to 20 senses. "
            "List every credible distinct English sense for the supplied "
            "entry, including common, literary, archaic, regional, and "
            "major technical senses. Exclude hyper-specialized jargon and "
            "do not split mere wording variants into separate senses. "
            "Each sense must contain exactly part_of_speech, definition, "
            "and example_sentence. Definitions must be concise and examples "
            "must demonstrate that sense. If the entry is not an English "
            "term or expression, return exactly "
            '{"status":"not_found"}.'
        ),
    },
    {
        "role": "user",
        "content": json.dumps(
            {"display_text": display_text},
            ensure_ascii=False,
        ),
    },
]
```

The term is JSON data, never interpolated into instructions. This avoids treating a malicious-looking lookup string as prompt policy.

- [ ] **Step 5: Make exactly one injected auxiliary call**

Use:

```python
response = await self._call_llm(
    task="vocabulary_definition",
    messages=messages,
    max_tokens=4000,
    temperature=0,
    tools=[],
)
```

The injected callable returns the extracted text string. `DefinitionProvider` owns no Hermes imports and performs no retry. Catch provider exceptions once and return `PROVIDER_ERROR`; parse the returned string once.

The router, not the provider, decides whether SQLite already contains the entry. This preserves the invariant that repeat lookups make zero model calls.

- [ ] **Step 6: Run definition tests**

```bash
python -m pytest tests/unit/test_definition.py -q
```

Expected: exact JSON parsing, all-sense preservation, invalid-response rejection, no-result behavior, and one-call enforcement pass.

- [ ] **Step 7: Commit the focused provider**

```text
Bound unseen-entry enrichment to one structured request

Constraint: Broad sense coverage cannot add conversational-agent turns
Rejected: Repair malformed model output | retries hide latency and nondeterminism
Confidence: high
Scope-risk: narrow
Directive: Keep entry text as JSON data and accept only the exact senses schema
Tested: focused definition parser and provider tests
```

### Task 6: Route the dedicated Telegram DM and register the plugin boundary

**Repository:** `/Users/ryanmuenker/Desktop/hermes`

**Files:**
- Modify: `src/hermes_vocab/config.py`
- Modify: `src/hermes_vocab/models.py`
- Modify: `src/hermes_vocab/review.py`
- Modify: `src/hermes_vocab/formatting.py`
- Create: `src/hermes_vocab/hermes_plugin/gateway.py`
- Modify: `src/hermes_vocab/hermes_plugin/hooks.py`
- Modify: `src/hermes_vocab/hermes_plugin/__init__.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_review.py`
- Modify: `tests/unit/test_formatting.py`
- Create: `tests/unit/test_gateway_routing.py`
- Modify: `tests/integration/test_hermes_plugin.py`

- [ ] **Step 1: Write failing scope and route tests**

Build `VocabularyGatewayRouter` with constructor-injected `CaptureService`, `ReviewService`, `DefinitionProvider`, and configured chat ID. Use a real temporary SQLite database and async fakes.

Required decline cases:

```python
assert asyncio.run(
    router.route(
        platform="discord",
        chat_id="7747352551",
        chat_type="dm",
        thread_id=None,
        user_message="perfidy",
    )
) is None
```

Also decline:

- Telegram with a different chat ID;
- the configured ID with any `chat_type` other than Hermes' canonical `dm`;
- any non-`None` `thread_id`, so Telegram topic lanes remain Hermes sessions;
- defensive slash-command input, even though Hermes core already bypasses it.

Required handled cases:

- empty input returns `Send a word or phrase.` and over-limit input returns `Send a word or phrase under 500 characters.`, both without model/storage writes;
- `Pro   Forma` preserves display text `Pro   Forma` while using normalized lookup key `pro forma`;
- pending review consumes the entire original message as the answer before any lookup or provider call;
- stored `pro forma` returns all stored senses with zero provider calls and zero writes;
- unseen `pro forma` makes one provider call, one batch save, and returns every committed sense;
- provider `NOT_FOUND`, malformed response, and exception each return deterministic user-safe copy and do not write;
- batch `STORAGE_ERROR` returns deterministic retry guidance and does not expose SQLite text;
- an injected `sqlite3.Error` from the initial lookup or the owner-task recheck returns `I couldn't save that. Please try again.`, leaves provider/capture fakes untouched, and stays handled;
- two simultaneous unseen requests for the same normalized entry share one provider task and one save;
- after the single-flight task completes, its map entry is removed;
- a cancelled waiter does not cancel the shared provider/save task;
- a later lookup reads SQLite with no provider call.

- [ ] **Step 2: Write failing pending-review storage tests**

The existing `ReviewService.has_pending_review()` turns `sqlite3.Error` into `False`. That is unsafe for a dedicated inbox: a review answer could be misclassified as a new lookup.

Replace the boolean contract with:

```python
class PendingReviewStatus(StrEnum):
    PENDING = "pending"
    NONE = "none"
    STORAGE_ERROR = "storage_error"
```

Add tests proving:

- a pending row returns `PENDING`;
- no row returns `NONE`;
- an injected SQLite failure returns `STORAGE_ERROR`;
- the router returns `I couldn't check your review. Please try again.` on `STORAGE_ERROR`;
- provider and capture fakes are untouched in that state.

Update `VocabularyHook` and its tests to use `pending_review_status()`. On non-dedicated surfaces, `STORAGE_ERROR` means no vocabulary prompt injection; the ordinary agent may proceed, while the dedicated route fails closed.

- [ ] **Step 3: Run the new tests and confirm red**

```bash
python -m pytest \
  tests/unit/test_config.py \
  tests/unit/test_review.py \
  tests/unit/test_formatting.py \
  tests/unit/test_gateway_routing.py \
  tests/integration/test_hermes_plugin.py \
  -q
```

Expected: router import/config/registration failures and the old boolean pending-review behavior.

- [ ] **Step 4: Add optional dedicated-chat configuration**

In `Settings`:

```python
telegram_chat_id: int | None = None
```

Parse `HERMES_VOCAB_TELEGRAM_CHAT_ID`:

- unset or blank → `None`;
- a base-10 integer, including negative Telegram IDs → `int`;
- any other value → `ConfigurationError` naming the variable.

The database and timezone remain required. An absent chat ID disables only deterministic inbound routing; tools, review cron, skill registration, and non-dedicated contextual guidance still load.

- [ ] **Step 5: Add exact aggregate formatting**

Expose:

```python
def format_entry(entry: VocabularyEntry, footer: str) -> str:
    ...
```

For one sense:

```text
Perfidy (noun)

Definition:
Betrayal of trust.

Example:
His perfidy ended the alliance.

✓ Saved.
```

For multiple senses:

```text
Pro Forma

1. adjective
Definition:
Provided as a matter of form.
Example:
The board issued a pro forma approval.

2. noun
Definition:
A projected financial statement.
Example:
The analyst prepared a pro forma.

✓ Saved.
```

Stored lookups use `Already saved.` as the footer. Preserve database order exactly. Do not prepend “Here is,” “Today's word,” tool names, model commentary, or an operation status.

The hook returns this single string. Telegram's existing adapter may split it only at the platform limit.

- [ ] **Step 6: Implement deterministic route ordering**

`VocabularyGatewayRouter.route()`:

```python
async def route(
    self,
    *,
    platform: str | None,
    sender_id: str | None = None,
    chat_id: str | None,
    chat_type: str | None,
    thread_id: str | None,
    user_message: str,
    **reply_metadata: object,
) -> str | None:
    ...
```

Order:

1. Decline unless platform is `telegram`, chat ID matches exactly after string conversion, chat type is Hermes' canonical `dm`, and thread ID is absent.
2. Decline slash commands defensively.
3. Ask `pending_review_status()`.
4. On `STORAGE_ERROR`, return review-check retry copy.
5. On `PENDING`, call `complete_review(user_message)` and return `format_review_completion()`; no lookup/model call.
6. Normalize the complete message with `normalize_entry_text()`. `EMPTY` returns `Send a word or phrase.`; `TOO_LONG` returns `Send a word or phrase under 500 characters.`.
7. Call `CaptureService.get_entry(normalized_text)` inside the storage-failure boundary. Existing aggregate returns `format_entry(..., "Already saved.")`; `sqlite3.Error` returns `I couldn't save that. Please try again.`.
8. For a miss, join or create the normalized entry's single-flight task.
9. Inside the owner task, recheck SQLite inside the same storage-failure boundary. Only a confirmed miss calls `DefinitionProvider.define(display_text)` once and `capture_entry(display_text, cards)` once. Format the aggregate actually returned by storage.

Exact failure copy:

```text
Empty: Send a word or phrase.
Over 500 code points: Send a word or phrase under 500 characters.
Not found: I couldn't define that. Please try another word or phrase.
Provider/validation failure: I couldn't define that. Please try again.
Storage failure: I couldn't save that. Please try again.
Review-state failure: I couldn't check your review. Please try again.
```

No branch includes an exception string.

- [ ] **Step 7: Implement cancellation-safe single-flight enrichment**

Maintain:

```python
self._inflight: dict[str, asyncio.Task[str]]
self._inflight_guard = asyncio.Lock()
```

Under the guard, reuse the existing task or create exactly one owner task for the normalized key. Await it through `asyncio.shield()` so cancelling one Telegram request does not cancel shared persistence.

The owner coroutine—not a waiter—owns cleanup. In its `finally` block, acquire `_inflight_guard` and remove the key only when the map still points to `asyncio.current_task()`. Therefore the map is cleaned even if every waiting request is cancelled before enrichment finishes.

The owner rechecks SQLite before calling the provider. This handles a process-local race with another writer and keeps “existing entry = zero model calls” true at the final serialization point.

Do not hold `_inflight_guard` across database or model I/O. Do not attach a cleanup callback that launches an untracked coroutine.

- [ ] **Step 8: Register the auxiliary task and hook adapter**

In `register(ctx)`, always register:

```python
ctx.register_auxiliary_task(
    key="vocabulary_definition",
    display_name="Vocabulary definition",
    description="Generate structured senses for an unseen vocabulary entry",
    defaults={"provider": "auto", "timeout": 60},
)
```

When `settings.telegram_chat_id is not None`:

1. lazily import `async_call_llm` and `extract_content_or_reasoning` inside the injected async caller;
2. call `async_call_llm(**kwargs)`;
3. return `extract_content_or_reasoning(response) or ""`;
4. construct `DefinitionProvider`, `VocabularyGatewayRouter`, and a small callback adapter;
5. register the adapter as `gateway_inbound_intercept`.

The adapter wraps handled text in Hermes' exact public type:

```python
async def intercept(**kwargs):
    text = await router.route(**kwargs)
    if text is None:
        return None
    from hermes_cli.plugins import GatewayInterceptResponse
    return GatewayInterceptResponse(text)
```

Do not register the inbound hook when the chat ID is absent. Retain tools, skill, and the legacy contextual `pre_llm_call` route. Do not widen `pre_llm_call` with chat inference: a handled gateway response guarantees dedicated non-command messages never reach agent creation or that later hook.

- [ ] **Step 9: Prove standalone plugin tests use a Hermes stub**

The vocabulary package does not declare Hermes as an install dependency. In `tests/integration/test_hermes_plugin.py`, install a minimal `hermes_cli.plugins` module in `sys.modules` containing the real-shape `GatewayInterceptResponse` dataclass before importing/reloading the plugin entry point.

Test both configurations:

- chat ID absent: two tools, `pre_llm_call`, skill, and auxiliary task register; inbound hook does not;
- chat ID present: same surfaces plus one `gateway_inbound_intercept`.

Invoke the registered callback with a stored `pro forma` fixture and assert:

```python
assert response == GatewayInterceptResponse(expected_exact_text)
call_llm.assert_not_awaited()
assert database_counts() == counts_before
```

Also assert the handled inbound callback completes without invoking an agent or the registered `pre_llm_call` callback.

- [ ] **Step 10: Run router and plugin regressions**

```bash
python -m pytest \
  tests/unit/test_config.py \
  tests/unit/test_review.py \
  tests/unit/test_formatting.py \
  tests/unit/test_gateway_routing.py \
  tests/unit/test_definition.py \
  tests/integration/test_hermes_plugin.py \
  -q
```

Expected: exact responses, root-DM scope, review precedence, full-message phrase lookup, zero-call cache hits, one-call misses, single-flight concurrency, registration, and standalone package imports pass.

- [ ] **Step 11: Commit deterministic vocabulary routing**

```text
Make the dedicated Telegram DM a deterministic vocabulary inbox

Constraint: Pending review owns the next message and topics remain normal Hermes lanes
Rejected: Reuse pre_llm_call | it cannot prevent session and conversational consumers
Confidence: high
Scope-risk: moderate
Directive: Preserve review -> normalize -> SQLite -> single-flight definition ordering
Tested: config, review, formatting, definition, router, and plugin integration tests
```

### Task 7: Document, configure, restart, and prove the live cutover

**Repository:** `/Users/ryanmuenker/Desktop/hermes`

**Files:**
- Modify: `README.md`
- Verify: `/Users/ryanmuenker/.hermes/.env` (local configuration, never commit)
- Verify: `/Users/ryanmuenker/.local/share/hermes-vocab/vocabulary.sqlite3`
- Verify: `/Users/ryanmuenker/.hermes/hermes-agent` checkout and gateway service

- [ ] **Step 1: Update architecture and behavior documentation**

Revise `README.md` before deployment:

- the complete non-command message in the configured Telegram root DM is the entry or expression;
- phrases such as `pro forma` are first-class entries;
- pending review retains first priority;
- stored entries use SQLite only;
- unseen entries make one focused multi-sense auxiliary request, not a general Hermes turn;
- every returned sense is validated and committed atomically;
- non-configured chats, Telegram topics, and slash commands retain normal Hermes behavior;
- schema migration 003 renames words to entries while preserving IDs/history;
- the local database remains authoritative;
- context-on-second-line still exists only on non-dedicated conversational surfaces;
- model-generated definitions may be inaccurate and should be checked for high-stakes use.

Update the diagram:

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
```

Document `HERMES_VOCAB_TELEGRAM_CHAT_ID` beside database/timezone configuration and state that leaving it unset disables deterministic DM routing.

- [ ] **Step 2: Run both repositories' focused and complete tests**

Hermes checkout:

```bash
cd /Users/ryanmuenker/.hermes/hermes-agent
python -m pytest \
  tests/hermes_cli/test_plugins.py \
  tests/gateway/test_telegram_topic_mode.py \
  tests/gateway/test_pre_gateway_dispatch.py \
  tests/gateway/test_unknown_command.py \
  tests/gateway/test_telegram_slash_confirm.py \
  -q
```

Vocabulary package:

```bash
cd /Users/ryanmuenker/Desktop/hermes
uv run --extra dev pytest
```

Then run Python diagnostics for both changed source trees and fix every new diagnostic.

- [ ] **Step 3: Back up the authoritative database correctly**

Stop the gateway so no new capture starts:

```bash
hermes gateway stop
```

Use SQLite's backup API, not a plain copy of the WAL-backed main file:

```bash
python3 - <<'PY'
from datetime import datetime
from pathlib import Path
import sqlite3

source = Path.home() / ".local/share/hermes-vocab/vocabulary.sqlite3"
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
target = source.with_name(f"vocabulary-before-v3-{stamp}.sqlite3")
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print(target)
PY
```

Open the backup read-only and record `PRAGMA integrity_check`, `PRAGMA user_version`, entry/sense/review counts, and the `perfidy` aggregate before upgrading.

- [ ] **Step 4: Build and install the vocabulary package**

```bash
cd /Users/ryanmuenker/Desktop/hermes
uv build
python3 -m zipfile -l dist/hermes_vocab-0.1.0-py3-none-any.whl
uv pip install \
  --python ~/.hermes/hermes-agent/venv/bin/python \
  --force-reinstall \
  dist/hermes_vocab-0.1.0-py3-none-any.whl
```

Wheel inspection must show `003_entry_terms.sql`, `gateway.py`, `definition.py`, and bundled `SKILL.md`.

- [ ] **Step 5: Configure the dedicated root DM**

In `~/.hermes/.env`, retain existing timezone/database values and add:

```bash
HERMES_VOCAB_TELEGRAM_CHAT_ID=7747352551
```

Use the actual allowlisted private Telegram DM ID if it differs. Keep file mode user-only. Never print or commit `TELEGRAM_BOT_TOKEN`.

Apply the approved quiet Telegram presentation settings:

```bash
hermes config set display.platforms.telegram.tool_progress off
hermes config set display.platforms.telegram.interim_assistant_messages off
```

Inspect the resulting non-secret `display.platforms.telegram` block in `~/.hermes/config.yaml` and confirm both values are `off`. The direct route emits no tool events, but these settings also suppress unrelated interim/tool output in the dedicated DM.

Run discovery without contacting a model:

```bash
HERMES_PLUGINS_DEBUG=1 hermes prompt-size
hermes plugins list --plain --no-bundled
hermes tools list --platform telegram
```

Expected: vocabulary plugin, two tools, skill, `pre_llm_call`, `gateway_inbound_intercept`, and `vocabulary_definition` auxiliary task all register without import errors.

- [ ] **Step 6: Start the migrated gateway and inspect state**

```bash
hermes gateway restart
hermes gateway status
hermes logs --since 5m
```

Verify:

- gateway service is supervised and running;
- plugin discovery has no errors;
- Telegram `tool_progress` and `interim_assistant_messages` both resolve to `off`;
- database `PRAGMA user_version` is 3;
- `PRAGMA foreign_key_check` is empty;
- pre-upgrade entry, sense, and review counts match;
- `perfidy` and every existing sense remain present in insertion order;
- the review cron still exists exactly once.

- [ ] **Step 7: Exercise deterministic behavior end to end**

In the configured Telegram root DM:

1. Send stored `perfidy`.
   - Expected: exact stored aggregate with `Already saved.`.
   - Logs: no main-agent API call, no auxiliary call, no tool progress.
2. Send unseen `pro forma`.
   - Expected: one numbered multi-sense response ending `✓ Saved.`.
   - Logs: exactly one `vocabulary_definition` auxiliary call and no main-agent API call.
3. Send `  PRO   FORMA  `.
   - Expected: the same committed aggregate ending `Already saved.`; no model call.
4. Send `/status`.
   - Expected: ordinary Hermes command behavior, not a vocabulary capture.
5. If a review is pending, answer it.
   - Expected: review completion and all stored senses; no model call.
6. Send ordinary text in a non-configured Hermes chat or Telegram topic lane.
   - Expected: normal Hermes behavior.

Capture the relevant timestamped log lines, response text, and post-run SQLite counts. Do not rely on visual impression alone.

- [ ] **Step 8: Smoke-test review scheduling after phrase capture**

Run the existing daily wrapper directly:

```bash
HERMES_TIMEZONE=Asia/Kuala_Lumpur \
HERMES_VOCAB_DB=/Users/ryanmuenker/.local/share/hermes-vocab/vocabulary.sqlite3 \
~/.hermes/hermes-agent/venv/bin/python \
~/.hermes/scripts/daily_review.py
```

Valid output is either one exact question (`What does '<entry>' mean?`) or empty stdout when today's event already exists. Then inspect:

```bash
hermes cron list
hermes cron status
```

Do not create a duplicate schedule.

- [ ] **Step 9: Commit documentation only**

Do not commit `.env`, database files, backups, logs, or credentials.

```text
Explain the deterministic vocabulary cutover and recovery path

Constraint: Operators must preserve a WAL-backed personal database during migration
Rejected: Document only the happy path | migration and route ownership are load-bearing
Confidence: high
Scope-risk: narrow
Directive: Keep the configured root DM, topics, commands, and conversational routes distinct
Tested: package build, plugin discovery, gateway restart, live Telegram, and review cron smoke
```

## Acceptance Checklist

- [ ] The configured Telegram root DM never enters the general Hermes agent or later pending/session consumers for non-command text.
- [ ] Authorization and original slash-command identity take precedence over interception.
- [ ] Slash commands, Telegram topic lanes, non-configured chats, and declined hooks retain normal Hermes behavior.
- [ ] A pending vocabulary review owns the complete message and finishes with no model request.
- [ ] Review-state storage failure cannot fall through to lookup or enrichment.
- [ ] The complete trimmed message—including `pro forma`—is normalized as one entry; there is no word parser or context syntax in the dedicated DM.
- [ ] Stored entries return every saved sense from SQLite in insertion-ID order with no model request or write.
- [ ] Unseen entries use exactly one focused auxiliary request per process-local single flight and one atomic batch-capture transaction.
- [ ] Every valid generated sense is returned and stored; malformed or partial batches leave no visible entry.
- [ ] Exact duplicate generated `(part_of_speech, definition)` pairs collapse before persistence while all remaining senses preserve model order.
- [ ] Concurrent captures converge on one complete committed aggregate.
- [ ] Schema migration 003 preserves entry IDs, sense IDs/order/content, timestamps, review status, events, and answers.
- [ ] Empty, oversized, explicit-not-found, provider/validation-failure, lookup/write-failure, and review-state-failure branches return their exact specified copy with no internal exception text.
- [ ] Telegram `tool_progress` and `interim_assistant_messages` are both `off`.
- [ ] Telegram displays one exact formatter response, split only by existing adapter limits, with no vocabulary tool progress or planning commentary.
- [ ] Hermes gateway interception is generic, scalar-only, transport-neutral, and contains no vocabulary-specific code.
- [ ] The vocabulary package remains independently testable without Hermes installed.
- [ ] Both repositories pass focused and complete regression suites, package diagnostics are clean, and no credentials/data/backups/logs are committed.
- [ ] The live gateway, `perfidy` cache hit, unseen `pro forma` capture, repeat phrase lookup, slash command, pending review, and daily cron path are smoke-tested with timestamped evidence.
