from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock
from pathlib import Path

import pytest
from hermes_vocab.capture import MAX_SOURCE_CONTEXT_LENGTH
from hermes_vocab.database import Database
from hermes_vocab.config import ConfigurationError, Settings
from hermes_vocab.formatting import format_entry
from hermes_vocab.hermes_plugin import register
from hermes_vocab.migrations.v005_backfill import backfill_v5
from hermes_vocab.review import ReviewService

@dataclass(frozen=True)
class PluginCommandSource:
    authenticated: bool
    platform: str
    chat_id: str
    chat_type: str
    thread_id: str | None
    sender_id: str | None = None


@dataclass(frozen=True)
class OutboundResponse:
    text: str
    correlation_id: str


@dataclass(frozen=True)
class GatewayInterceptResponse:
    text: str
    correlation_id: str | None = None


SOURCE_ERROR = (
    "Vocabulary study is available only in the configured Telegram root DM."
)


def root_command_source() -> PluginCommandSource:
    return PluginCommandSource(
        authenticated=True,
        platform="telegram",
        chat_id="7747352551",
        chat_type="dm",
        thread_id=None,
    )

# Names Hermes' own command registry owns; plugin registrations never win.
RESERVED_COMMAND_NAMES = frozenset({"exit", "quit", "stop", "help", "status"})


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.tool_schemas: dict[str, dict] = {}
        self.toolsets: dict[str, str] = {}
        self.hooks: dict[str, object] = {}
        self.skills: dict[str, Path] = {}
        self.auxiliary_tasks: dict[str, dict] = {}
        self.tool_is_async: dict[str, bool] = {}
        self.commands: dict[str, object] = {}
        self.command_descriptions: dict[str, str] = {}
        self.command_args_hints: dict[str, str] = {}
        self.command_source_aware: dict[str, bool] = {}

    def register_tool(
        self, *, name, toolset, schema, handler, is_async: bool = False
    ) -> None:
        self.tools[name] = handler
        self.tool_schemas[name] = schema
        self.toolsets[name] = toolset
        self.tool_is_async[name] = is_async

    def register_command(
        self,
        name,
        handler,
        description: str = "",
        args_hint: str = "",
        source_aware: bool = False,
    ) -> None:
        # Hermes reserves its built-in command names; a plugin that claims one
        # is silently dropped at runtime, so refuse it here too.
        if name in RESERVED_COMMAND_NAMES:
            raise AssertionError(
                f"/{name} collides with a Hermes built-in command"
            )
        self.commands[name] = handler
        self.command_descriptions[name] = description
        self.command_args_hints[name] = args_hint
        self.command_source_aware[name] = source_aware

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback

    def register_skill(self, name, path) -> None:
        self.skills[name] = Path(path)
    def register_auxiliary_task(
        self,
        *,
        key,
        display_name,
        description,
        defaults,
    ) -> None:
        self.auxiliary_tasks[key] = {
            "display_name": display_name,
            "description": description,
            "defaults": defaults,
        }




def install_auxiliary_client(monkeypatch, response: str) -> AsyncMock:
    call_llm = AsyncMock(return_value=response)
    auxiliary = types.ModuleType("agent.auxiliary_client")
    auxiliary.async_call_llm = call_llm
    auxiliary.extract_content_or_reasoning = lambda value: value
    agent = types.ModuleType("agent")
    agent.auxiliary_client = auxiliary
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    return call_llm


VALID_HOOKS = frozenset(
    {
        "pre_llm_call",
        "gateway_inbound_intercept",
        "post_outbound_delivery",
    }
)


def register_plugin(
    monkeypatch,
    tmp_path: Path,
    *,
    chat_id: int | None = None,
    valid_hooks: frozenset[str] = VALID_HOOKS,
    omit_contracts: tuple[str, ...] = (),
) -> tuple[FakeContext, Path]:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    monkeypatch.setenv("HERMES_VOCAB_DB", str(path))
    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
    if chat_id is None:
        monkeypatch.delenv("HERMES_VOCAB_TELEGRAM_CHAT_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_VOCAB_TELEGRAM_CHAT_ID", str(chat_id))
    contracts = types.ModuleType("hermes_cli.plugin_contracts")
    contracts.PluginCommandSource = PluginCommandSource
    contracts.OutboundResponse = OutboundResponse
    contracts.GatewayInterceptResponse = GatewayInterceptResponse
    for name in omit_contracts:
        delattr(contracts, name)
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.VALID_HOOKS = set(valid_hooks)
    plugins.GatewayInterceptResponse = GatewayInterceptResponse
    hermes_cli = types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_contracts", contracts)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    context = FakeContext()
    register(context)
    return context, path


def hook_call(callback, message: str, platform: str = "telegram"):
    return callback(
        session_id="session",
        user_message=message,
        conversation_history=[],
        is_first_turn=True,
        model="test-model",
        platform=platform,
    )


def save_args(
    operation: str,
    *,
    display_text: str = "bank",
    definition: str = "A financial institution.",
    context: str | None = None,
    matching_sense_id: int | None = None,
) -> dict:
    args = {"display_text": display_text, "operation": operation}
    if operation != "existing_sense":
        args.update(
            {
                "part_of_speech": "noun",

                "definition": definition,
                "example_sentence": "She visited the bank.",
            }
        )
    if context is not None:
        args["source_context"] = context
    if matching_sense_id is not None:
        args["matching_sense_id"] = matching_sense_id
    return args

def test_review_command_is_source_aware_and_mutates_only_configured_root_dm(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    for index in range(2):
        context.tools["vocabulary_save_card"](
            {
                "display_text": f"word-{index}",
                "operation": "new_entry",
                "part_of_speech": "noun",
                "definition": f"Definition {index}.",
                "example_sentence": f"Example {index}.",
            }
        )
    with Database(path).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        backfill_v5(connection)
        connection.commit()
    unauthorized = {
        "cli": PluginCommandSource(
            authenticated=True,
            platform="cli",
            chat_id="7747352551",
            chat_type="dm",
            thread_id=None,
        ),
        "other telegram chat": PluginCommandSource(
            authenticated=True,
            platform="telegram",
            chat_id="1",
            chat_type="dm",
            thread_id=None,
        ),
        "group chat": PluginCommandSource(
            authenticated=True,
            platform="telegram",
            chat_id="7747352551",
            chat_type="group",
            thread_id=None,
        ),
        "forum thread": PluginCommandSource(
            authenticated=True,
            platform="telegram",
            chat_id="7747352551",
            chat_type="dm",
            thread_id="topic-7",
        ),
        "unauthenticated root dm": PluginCommandSource(
            authenticated=False,
            platform="telegram",
            chat_id="7747352551",
            chat_type="dm",
            thread_id=None,
        ),
    }
    root = root_command_source()

    for label, source in unauthorized.items():
        assert context.commands["review"]("", source=source) == SOURCE_ERROR, label
        assert context.commands["test"]("forward", source=source) == SOURCE_ERROR, label
    with Database(path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM study_prompts").fetchone()[0] == 0
    started = context.commands["review"]("", source=root)

    assert context.command_source_aware["review"] is True
    assert context.command_source_aware["test"] is True
    assert "post_outbound_delivery" in context.hooks
    assert isinstance(started, OutboundResponse)
    assert started.text.startswith("Review 1 of ")
    assert started.correlation_id.startswith("review:")

    for label, source in unauthorized.items():
        assert context.commands["review"]("", source=source) == SOURCE_ERROR, label
        assert context.commands["test"]("forward", source=source) == SOURCE_ERROR, label
    resumed = context.commands["review"]("", source=root)

    assert isinstance(resumed, OutboundResponse)
    assert resumed.correlation_id == started.correlation_id
    with Database(path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM study_prompts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 0


def test_registration_exposes_tools_hook_skill_and_auxiliary_task(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)

    assert set(context.tools) == {"vocabulary_save_card", "vocabulary_continue_study"}
    assert context.commands == {}
    assert set(context.hooks) == {"pre_llm_call"}
    assert set(context.skills) == {"vocabulary"}
    assert context.auxiliary_tasks == {
        "vocabulary_definition": {
            "display_name": "Vocabulary definition",
            "description": "Generate structured senses for an unseen vocabulary entry",
            "defaults": {"provider": "auto", "timeout": 60},
        },
        "vocabulary_answer_evaluation": {
            "display_name": "Vocabulary answer evaluation",
            "description": "Evaluate a learner response against stored vocabulary senses",
            "defaults": {"provider": "auto", "timeout": 60},
        },
    }
    assert context.skills["vocabulary"].name == "SKILL.md"
    assert context.toolsets == {
        "vocabulary_save_card": "vocabulary",
        "vocabulary_continue_study": "vocabulary",
    }
    assert context.tool_is_async == {
        "vocabulary_save_card": False,
        "vocabulary_continue_study": True,
    }
    continue_schema = context.tool_schemas["vocabulary_continue_study"]
    assert "currently delivered study prompt" in continue_schema["description"]
    assert continue_schema["parameters"]["required"] == ["answer_text"]
    save_schema = context.tool_schemas["vocabulary_save_card"]["parameters"]
    assert save_schema["required"] == ["display_text", "operation"]
    assert save_schema["properties"]["operation"] == {
        "type": "string",
        "enum": ["new_entry", "new_sense", "existing_sense"],
    }
    assert save_schema["properties"]["source_context"] == {"type": "string"}
    assert save_schema["properties"]["matching_sense_id"] == {"type": "integer"}

def test_generic_study_tool_uses_shared_no_active_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)

    payload = json.loads(
        asyncio.run(
            context.tools["vocabulary_continue_study"](
                {"answer_text": "an answer"}
            )
        )
    )

    assert payload == {
        "status": "no_active",
        "allowed_ratings": [],
        "text": "There isn't a delivered study prompt waiting.",
    }


def test_directional_test_registration_advertises_explicit_modes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)

    assert context.command_descriptions["test"] == (
        "Start or resume a five-card directional vocabulary test"
    )
    assert context.command_args_hints["test"] == "forward|reverse"



def test_directional_test_command_matrix_keeps_bare_and_invalid_inputs_pure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)

    usage = (
        "Usage: /test forward|reverse\n"
        "Forward: recall each saved meaning from its word.\n"
        "Reverse: recall the saved word from one exact definition."
    )
    assert context.commands["test"]("", source=root_command_source()) == usage
    assert context.commands["test"]("   ", source=root_command_source()) == usage
    assert context.commands["test"]("sideways", source=root_command_source()) == usage
    assert context.commands["test"]("forward now", source=root_command_source()) == usage
    with Database(path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM study_queue").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("argument", "expected_question"),
    [
        ("forward", "Question 1 of 5\nWhat does 'word-0' mean?"),
        (
            "reverse",
            "Question 1 of 5\nWhich saved word matches this definition?\n"
            "Definition 0.",
        ),
    ],
)
def test_explicit_directional_test_command_starts_and_resumes_v5_queue(
    monkeypatch,
    tmp_path: Path,
    argument: str,
    expected_question: str,
) -> None:
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    for index in range(5):
        context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text=f"word-{index}",
                definition=f"Definition {index}.",
            )
        )
    with Database(path).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        backfill_v5(connection)
        connection.commit()

    first = context.commands["test"](argument, source=root_command_source())
    duplicate = context.commands["test"](f"  {argument}  ", source=root_command_source())

    assert first.text == expected_question
    assert duplicate.text == expected_question
    with Database(path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM study_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM study_queue").fetchone()[0] == 5
        assert connection.execute(
            "SELECT mode FROM study_sessions"
        ).fetchone()[0] == f"test_{argument}"
        assert connection.execute(
            "SELECT status FROM study_prompts"
        ).fetchone()[0] == "prepared"

@pytest.mark.parametrize(
    ("argument", "answer", "next_question"),
    [
        (
            "forward",
            "first answer",
            "Question 2 of 5\nWhat does 'word-1' mean?",
        ),
        (
            "reverse",
            "word-0",
            "Question 2 of 5\nWhich saved word matches this definition?\n"
            "Definition 1.",
        ),
    ],
)
def test_directional_rating_returns_schedule_then_prepared_next_question(
    monkeypatch,
    tmp_path: Path,
    argument: str,
    answer: str,
    next_question: str,
) -> None:
    install_auxiliary_client(
        monkeypatch,
        '{"grade":"correct","feedback":"Correct."}',
    )
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    for index in range(5):
        context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text=f"word-{index}",
                definition=f"Definition {index}.",
            )
        )
    database = Database(path)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        backfill_v5(connection)
        connection.commit()
    context.commands["test"](argument, source=root_command_source())
    with database.connect() as connection:
        prompt_id = connection.execute(
            "SELECT id FROM study_prompts WHERE status = 'prepared'"
        ).fetchone()[0]
    delivered = ReviewService(
        database,
        Settings.from_environment().timezone,
    ).record_delivery(
        prompt_id,
        delivery_id="delivery-1",
        content_fingerprint="fingerprint-1",
    )
    assert delivered is not None

    async def continue_study(text: str) -> dict:
        return json.loads(
            await context.tools["vocabulary_continue_study"](
                {"answer_text": text}
            )
        )

    answered = asyncio.run(continue_study(answer))
    assert answered["status"] == "awaiting_rating"
    rated = asyncio.run(continue_study("good"))

    assert rated["status"] == "finalized"
    assert rated["text"].splitlines()[:3] == [
        "Rated: Good",
        rated["text"].splitlines()[1],
        "Progress: 1 of 5 complete.",
    ]
    assert rated["text"].splitlines()[1].startswith("Next due: ")
    assert rated["text"].endswith(f"\n\n{next_question}")
    with database.connect() as connection:
        next_prompt = connection.execute(
            """
            SELECT status FROM study_prompts
            WHERE id != ? ORDER BY id DESC LIMIT 1
            """,
            (prompt_id,),
        ).fetchone()
        assert next_prompt["status"] == "prepared"
        assert connection.execute(
            """
            SELECT COUNT(*) FROM prompt_delivery_attempts
            WHERE prompt_id != ?
            """,
            (prompt_id,),
        ).fetchone()[0] == 0

@pytest.mark.parametrize(
    ("argument", "answer", "provider_response", "expected_calls", "reveal"),
    [
        (
            "forward",
            "wrong meaning",
            '{"grade":"incorrect","feedback":"Not the saved meaning."}',
            1,
            "Definition:\nDefinition 0.\n\nExample:\nShe visited the bank.",
        ),
        (
            "forward",
            "show answer",
            '{"grade":"correct","feedback":"unused"}',
            0,
            "Definition:\nDefinition 0.\n\nExample:\nShe visited the bank.",
        ),
        (
            "forward",
            "idk",
            '{"grade":"correct","feedback":"unused"}',
            0,
            "Definition:\nDefinition 0.\n\nExample:\nShe visited the bank.",
        ),
        (
            "reverse",
            "wrong word",
            '{"grade":"correct","feedback":"unused"}',
            0,
            "Answer: word-0\n\nDefinition:\nDefinition 0.",
        ),
        (
            "reverse",
            "show answer",
            '{"grade":"correct","feedback":"unused"}',
            0,
            "Answer: word-0\n\nDefinition:\nDefinition 0.",
        ),
        (
            "reverse",
            "idk",
            '{"grade":"correct","feedback":"unused"}',
            0,
            "Answer: word-0\n\nDefinition:\nDefinition 0.",
        ),
    ],
)
def test_auto_again_response_reveals_evaluation_before_schedule_and_next_prompt(
    monkeypatch,
    tmp_path: Path,
    argument: str,
    answer: str,
    provider_response: str,
    expected_calls: int,
    reveal: str,
) -> None:
    call_llm = install_auxiliary_client(monkeypatch, provider_response)
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    for index in range(5):
        context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text=f"word-{index}",
                definition=f"Definition {index}.",
            )
        )
    database = Database(path)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        backfill_v5(connection)
        connection.commit()
    context.commands["test"](argument, source=root_command_source())
    with database.connect() as connection:
        prompt_id = connection.execute(
            "SELECT id FROM study_prompts WHERE status = 'prepared'"
        ).fetchone()[0]
    assert ReviewService(
        database,
        Settings.from_environment().timezone,
    ).record_delivery(
        prompt_id,
        delivery_id="delivery",
        content_fingerprint="fingerprint",
    ) is not None

    payload = json.loads(
        asyncio.run(
            context.tools["vocabulary_continue_study"](
                {"answer_text": answer}
            )
        )
    )

    assert payload["status"] == "finalized"
    assert payload["text"].startswith("Grade: Incorrect\nFeedback: ")
    assert reveal in payload["text"]
    assert payload["text"].index("Grade: Incorrect") < payload["text"].index(reveal)
    assert payload["text"].index(reveal) < payload["text"].index("Rated: Again")
    assert payload["text"].endswith("\nWhat does 'word-1' mean?") if argument == "forward" else payload["text"].endswith("\nDefinition 1.")
    assert "Choose effort:" not in payload["text"]
    assert call_llm.await_count == expected_calls










def test_dedicated_chat_registers_and_handles_stored_entry_without_model_or_agent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    @dataclass(frozen=True, slots=True)
    class GatewayInterceptResponse:
        text: str

    call_llm = AsyncMock()
    auxiliary = types.ModuleType("agent.auxiliary_client")
    auxiliary.async_call_llm = call_llm
    auxiliary.extract_content_or_reasoning = lambda response: response
    agent = types.ModuleType("agent")
    agent.auxiliary_client = auxiliary
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.GatewayInterceptResponse = GatewayInterceptResponse
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.plugins = plugins
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    saved = json.loads(
        context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text="pro forma",
                definition="Provided as a matter of form.",
            )
        )
    )
    with Database(path).connect() as connection:
        counts_before = (
            connection.execute("SELECT COUNT(*) FROM vocabulary_entries").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0],
        )
    original_pre_llm = context.hooks["pre_llm_call"]
    pre_llm = Mock(wraps=original_pre_llm)
    context.hooks["pre_llm_call"] = pre_llm

    response = asyncio.run(
        context.hooks["gateway_inbound_intercept"](
            platform="telegram",
            sender_id="42",
            chat_id="7747352551",
            chat_type="dm",
            thread_id=None,
            user_message="PRO   FORMA",
        )
    )

    entry = original_pre_llm.__self__.capture_service.get_entry("pro forma")
    assert saved["status"] == "saved"
    assert response.text == format_entry(entry, "Already saved.")
    call_llm.assert_not_awaited()
    pre_llm.assert_not_called()
    with Database(path).connect() as connection:
        counts_after = (
            connection.execute("SELECT COUNT(*) FROM vocabulary_entries").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0],
        )
    assert counts_after == counts_before


def test_telegram_single_word_injects_capture_guidance(monkeypatch, tmp_path: Path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    callback = context.hooks["pre_llm_call"]

    result = hook_call(callback, "obdurate")

    assert "vocabulary:vocabulary" in result
    assert "vocabulary_save_card" in result
    assert "vocabulary_save_card" in hook_call(callback, "what does this mean?")
    assert hook_call(callback, "obdurate", platform="cli") is None


def test_capture_hook_never_reads_study_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    callback = context.hooks["pre_llm_call"]

    def fail(*args, **kwargs):
        raise AssertionError("capture guidance must not read study state")

    monkeypatch.setattr(callback.__self__.review_service, "snapshot", fail)
    monkeypatch.setattr(callback.__self__.review_service, "answerable_prompt", fail)

    assert "vocabulary_save_card" in hook_call(callback, "perfidy")


def test_contextual_capture_injects_verbatim_json_and_empty_senses(
    monkeypatch, tmp_path: Path
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    source_context = 'She said "bank".\nThen watched the river.'

    guidance = hook_call(
        context.hooks["pre_llm_call"],
        f"bank\n{source_context}",
    )

    request_state = {
        "display_text": "bank",
        "context": source_context,
        "senses": [],
    }
    assert json.dumps(request_state, ensure_ascii=False) in guidance
    assert "Choose exactly one operation" in guidance
    assert "exactly once" not in guidance
    assert "Make one initial vocabulary_save_card call" in guidance
    assert (
        "If the initial call's status is conflict, use its returned state to "
        "make one corrected second call"
    ) in guidance
    assert "relay its text value verbatim" in guidance


def test_capture_lookup_storage_error_injects_only_retry_guidance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    callback = context.hooks["pre_llm_call"]

    def fail_lookup(display_text: str):
        raise sqlite3.OperationalError(f"private state for {display_text}")

    monkeypatch.setattr(callback.__self__.capture_service, "get_entry", fail_lookup)

    guidance = hook_call(callback, "bank")

    assert guidance == (
        "Do not call vocabulary_save_card. Reply exactly: "
        "I couldn't save that entry. Please try again."
    )
    assert "private state" not in guidance


def test_oversized_context_injects_only_exact_rejection_guidance(
    monkeypatch, tmp_path: Path
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    oversized_context = "x" * (MAX_SOURCE_CONTEXT_LENGTH + 1)
    guidance = hook_call(
        context.hooks["pre_llm_call"],
        f"bank\n{oversized_context}",
    )

    assert guidance == (
        "Do not call vocabulary_save_card. Reply exactly: "
        f"Context is too long. Keep it under {MAX_SOURCE_CONTEXT_LENGTH} characters."
    )
    assert oversized_context not in guidance


def test_existing_word_guidance_contains_numbered_sense_id(
    monkeypatch, tmp_path: Path
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    saved = json.loads(
        context.tools["vocabulary_save_card"](save_args("new_entry"))
    )

    guidance = hook_call(context.hooks["pre_llm_call"], "bank")

    assert saved["status"] == "saved"
    assert '"context": null' in guidance
    assert '"id": 1' in guidance
    assert '"part_of_speech": "noun"' in guidance
    assert '"definition": "A financial institution."' in guidance


def test_save_tool_returns_exact_formatted_text_and_persists(monkeypatch, tmp_path: Path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)

    payload = json.loads(
        context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text="obdurate",
                definition="Stubbornly refusing to change one's opinion.",
            )
        )
    )

    assert payload["status"] == "saved"
    assert payload["text"].endswith("✓ Saved.")
    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_senses"
        ).fetchone()[0] == 1


def test_new_sense_tool_appends_verbatim_context(monkeypatch, tmp_path: Path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    tool = context.tools["vocabulary_save_card"]
    tool(save_args("new_entry"))
    source_context = "She sat on the bank and watched the river."

    result = json.loads(
        tool(
            save_args(
                "new_sense",
                definition="Land alongside a river.",
                context=source_context,
            )
        )
    )

    assert result["status"] == "new_sense_saved"
    assert result["text"].endswith("✓ New meaning saved.")
    with Database(path).connect() as connection:
        row = connection.execute(
            "SELECT source_context FROM vocabulary_senses ORDER BY id DESC"
        ).fetchone()
    assert row[0] == source_context


def test_existing_sense_tool_performs_no_write(monkeypatch, tmp_path: Path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    tool = context.tools["vocabulary_save_card"]
    tool(save_args("new_entry"))

    result = json.loads(
        tool(save_args("existing_sense", matching_sense_id=1))
    )

    assert result["status"] == "already_exists"
    assert result["text"].endswith("Already saved with this meaning.")
    assert "state" not in result
    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_senses"
        ).fetchone()[0] == 1


def test_cross_word_sense_id_returns_refreshed_conflict(
    monkeypatch, tmp_path: Path
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    tool = context.tools["vocabulary_save_card"]
    tool(save_args("new_entry"))
    tool(
        save_args(
            "new_entry",
            display_text="shore",
            definition="Land at the edge of water.",
        )
    )

    result = json.loads(
        tool(save_args("existing_sense", matching_sense_id=2))
    )

    assert result == {
        "status": "conflict",
        "text": "That entry changed while I was saving it. Please try again.",
        "state": {
            "entry_exists": True,
            "senses": [
                {
                    "id": 1,
                    "part_of_speech": "noun",
                    "definition": "A financial institution.",
                }
            ],
        },
    }


def test_invalid_save_arguments_do_not_write(monkeypatch, tmp_path: Path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    tool = context.tools["vocabulary_save_card"]
    invalid_payloads = [
        save_args("not_an_operation"),
        {**save_args("new_entry"), "display_text": 123},
        {**save_args("new_entry"), "source_context": 123},
        {**save_args("new_entry"), "matching_sense_id": "1"},
        {**save_args("new_entry"), "definition": 123},
    ]

    results = [json.loads(tool(payload)) for payload in invalid_payloads]

    assert results == [
        {
            "status": "invalid",
            "text": "Send a word or expression, optionally followed by context on the next line.",
        }
    ] * len(invalid_payloads)
    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_senses"
        ).fetchone()[0] == 0


def test_non_telegram_multiline_message_is_not_auto_capture(
    monkeypatch, tmp_path: Path
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)

    assert (
        hook_call(
            context.hooks["pre_llm_call"],
            "bank\nShe sat beside the river.",
            platform="cli",
        )
        is None
    )


def test_existing_senses_survive_plugin_restart(monkeypatch, tmp_path: Path) -> None:
    first_context, path = register_plugin(monkeypatch, tmp_path)
    save = first_context.tools["vocabulary_save_card"]
    save(save_args("new_entry"))
    save(
        save_args(
            "new_sense",
            definition="Land alongside a river.",
            context="They rested beside the river bank.",
        )
    )

    restarted = FakeContext()
    register(restarted)
    guidance = hook_call(restarted.hooks["pre_llm_call"], "bank")

    assert path.exists()
    assert '"id": 1' in guidance
    assert '"definition": "A financial institution."' in guidance
    assert '"id": 2' in guidance
    assert '"definition": "Land alongside a river."' in guidance


def test_multi_sense_capture_survives_restart(
    monkeypatch, tmp_path: Path
) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    save = context.tools["vocabulary_save_card"]

    new_word = json.loads(
        save(
            {
                "display_text": "bank",
                "operation": "new_entry",
                "part_of_speech": "noun",
                "definition": "A financial institution.",
                "example_sentence": "She deposited the cheque at the bank.",
            }
        )
    )
    distinct_sense = json.loads(
        save(
            {
                "display_text": "bank",
                "operation": "new_sense",
                "source_context": "She sat on the bank and watched the river.",
                "part_of_speech": "noun",
                "definition": "Land alongside a river.",
                "example_sentence": "They rested on the grassy bank.",
            }
        )
    )
    with Database(path).connect() as connection:
        financial_sense_id = connection.execute(
            """
            SELECT id FROM vocabulary_senses
            WHERE definition = 'A financial institution.'
            """
        ).fetchone()[0]
    existing_sense = json.loads(
        save(
            {
                "display_text": "bank",
                "operation": "existing_sense",
                "source_context": "She went to the bank to deposit a cheque.",
                "matching_sense_id": financial_sense_id,
            }
        )
    )

    assert new_word["status"] == "saved"
    assert distinct_sense["status"] == "new_sense_saved"
    assert existing_sense["status"] == "already_exists"
    with Database(path).connect() as connection:
        sense_ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT id FROM vocabulary_senses ORDER BY id"
            ).fetchall()
        )
    assert len(sense_ids) == 2

    restarted_context, restarted_path = register_plugin(monkeypatch, tmp_path)
    assert restarted_path == path
    capture_guidance = hook_call(
        restarted_context.hooks["pre_llm_call"],
        "bank",
    )
    for sense_id in sense_ids:
        assert f'"id": {sense_id}' in capture_guidance

    with Database(restarted_path).connect() as connection:
        reopened_counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM vocabulary_entries),
                (SELECT COUNT(*) FROM vocabulary_senses)
            """
        ).fetchone()

    assert tuple(reopened_counts) == (1, 2)


def test_registered_test_cross_path_survives_restart_and_evaluator_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    call_llm = install_auxiliary_client(monkeypatch, "")
    call_llm.side_effect = [
        '{"grade":"correct","feedback":"Feedback 1."}',
        RuntimeError("provider unavailable"),
        '{"grade":"partial","feedback":"Feedback 2."}',
        '{"grade":"correct","feedback":"Feedback 3."}',
        '{"grade":"partial","feedback":"Feedback 4."}',
        '{"grade":"incorrect","feedback":"Feedback 5."}',
        '{"grade":"incorrect","feedback":"Retry feedback."}',
    ]
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    for index in range(5):
        context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text=f"word-{index}",
                definition=f"Definition {index}.",
            )
        )
    database = Database(path)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        backfill_v5(connection)
        connection.commit()

    def deliver_current(index: int) -> None:
        with database.connect() as connection:
            prompt_id = connection.execute(
                """
                SELECT id FROM study_prompts
                WHERE status = 'prepared' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()[0]
        delivered = ReviewService(
            database,
            Settings.from_environment().timezone,
        ).record_delivery(
            prompt_id,
            delivery_id=f"delivery-{index}",
            content_fingerprint=f"fingerprint-{index}",
        )
        assert delivered is not None

    def continue_study(active_context: FakeContext, text: str) -> dict:
        return json.loads(
            asyncio.run(
                active_context.tools["vocabulary_continue_study"](
                    {"answer_text": text}
                )
            )
        )

    assert context.commands["test"]("forward", source=root_command_source()).text == (
        "Question 1 of 5\nWhat does 'word-0' mean?"
    )
    deliver_current(1)
    first_answer = continue_study(context, "answer 1")
    assert first_answer["status"] == "awaiting_rating"
    first_rating = continue_study(context, "good")
    assert first_rating["status"] == "finalized"
    assert first_rating["text"].endswith(
        "\n\nQuestion 2 of 5\nWhat does 'word-1' mean?"
    )
    assert context.commands["test"]("forward", source=root_command_source()).text == (
        "Question 2 of 5\nWhat does 'word-1' mean?"
    )

    deliver_current(2)
    failed = continue_study(context, "discarded attempt")
    assert failed == {
        "status": "evaluation_error",
        "allowed_ratings": [],
        "text": (
            "I couldn't evaluate that answer, and nothing was recorded. "
            "Send your answer again — the next message you send is graded "
            "as your answer."
        ),
    }
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 1

    restarted = FakeContext()
    register(restarted)
    assert restarted.commands["test"]("forward", source=root_command_source()).text == (
        "Question 2 of 5\nWhat does 'word-1' mean?"
    )
    second_answer = continue_study(restarted, "answer 2")
    assert second_answer["status"] == "awaiting_rating"
    assert continue_study(restarted, "hard")["status"] == "finalized"

    deliver_current(3)
    assert continue_study(restarted, "answer 3")["status"] == "awaiting_rating"
    assert continue_study(restarted, "good")["status"] == "finalized"
    deliver_current(4)
    assert continue_study(restarted, "answer 4")["status"] == "awaiting_rating"
    assert continue_study(restarted, "hard")["status"] == "finalized"
    deliver_current(5)
    fifth = continue_study(restarted, "answer 5")
    assert fifth["status"] == "finalized"
    assert fifth["text"].startswith(
        "Grade: Incorrect\nFeedback: Feedback 5."
    )
    assert fifth["text"].index("Grade: Incorrect") < fifth["text"].index(
        "Rated: Again"
    )
    assert fifth["text"].endswith(
        "\n\nQuestion 5 of 5 · retry\nWhat does 'word-4' mean?"
    )
    assert restarted.commands["test"]("forward", source=root_command_source()).text.startswith(
        "Question 5 of 5 · retry\n"
    )
    deliver_current(6)
    completed = continue_study(restarted, "retry answer")

    assert completed["status"] == "finalized"
    assert completed["allowed_ratings"] == []
    assert completed["text"].startswith(
        "Grade: Incorrect\nFeedback: Retry feedback."
    )
    assert completed["text"].endswith(
        "Forward test complete.\n"
        "Results: 2 correct, 2 partial, 1 incorrect."
    )
    assert completed["text"].index("Grade: Incorrect") < completed["text"].index(
        "Forward test complete."
    )
    duplicate = continue_study(restarted, "retry answer")
    assert duplicate["status"] == "no_active"
    assert "Grade:" not in duplicate["text"]
    assert "Feedback:" not in duplicate["text"]
    assert [call.kwargs["task"] for call in call_llm.await_args_list] == [
        "vocabulary_answer_evaluation"
    ] * 7
    with database.connect() as connection:
        assert connection.execute(
            "SELECT status FROM study_sessions"
        ).fetchone()[0] == "completed"
        assert connection.execute(
            "SELECT COUNT(*) FROM answer_drafts"
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM review_attempts"
        ).fetchone()[0] == 6
        assert connection.execute(
            """
            SELECT COUNT(*) FROM vocabulary_cards
            WHERE direction = 'forward' AND repetitions >= 1
            """
        ).fetchone()[0] == 5


@pytest.mark.parametrize(
    ("incompatibility", "message"),
    [
        (
            {"valid_hooks": frozenset({"pre_llm_call", "gateway_inbound_intercept"})},
            "Installed Hermes does not expose the outbound receipt hook",
        ),
        (
            {"omit_contracts": ("PluginCommandSource",)},
            "Installed Hermes does not expose delivery-safe plugin contracts",
        ),
        (
            {"omit_contracts": ("GatewayInterceptResponse",)},
            "Installed Hermes does not expose delivery-safe plugin contracts",
        ),
        (
            {"omit_contracts": ("OutboundResponse",)},
            "Installed Hermes does not expose delivery-safe plugin contracts",
        ),
    ],
)
def test_incompatible_hermes_registers_no_mutating_command_or_study_state(
    monkeypatch,
    tmp_path: Path,
    incompatibility: dict,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError) as error:
        register_plugin(
            monkeypatch,
            tmp_path,
            chat_id=7747352551,
            **incompatibility,
        )

    assert str(error.value) == message
    assert not (tmp_path / "data" / "vocabulary.sqlite3").exists()


def test_endstudy_command_ends_study_without_changing_unanswered_schedules(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """R5: shipped copy tells learners to exit, so an exit affordance must exist."""
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    for index in range(2):
        context.tools["vocabulary_save_card"](
            {
                "display_text": f"word-{index}",
                "operation": "new_entry",
                "part_of_speech": "noun",
                "definition": f"Definition {index}.",
                "example_sentence": f"Example {index}.",
            }
        )
    database = Database(path)

    assert context.commands["endstudy"]("", source=root_command_source()) == (
        "There is no active vocabulary study session."
    )

    context.commands["review"]("", source=root_command_source())
    with database.connect() as connection:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, state, due_at, effective_due_at FROM vocabulary_cards"
                " ORDER BY id"
            )
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM study_sessions WHERE status = 'active'"
        ).fetchone()[0] == 1

    assert context.commands["endstudy"]("", source=root_command_source()) == (
        "Review exited. Unfinished cards are still due."
    )

    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM study_sessions WHERE status = 'active'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM review_attempts"
        ).fetchone()[0] == 0
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT id, state, due_at, effective_due_at FROM vocabulary_cards"
                " ORDER BY id"
            )
        ] == before

    resumed = context.commands["review"]("", source=root_command_source())
    assert "word-" in str(resumed)


def test_endstudy_command_rejects_sources_outside_the_root_dm(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    context.tools["vocabulary_save_card"](
        {
            "display_text": "word-0",
            "operation": "new_entry",
            "part_of_speech": "noun",
            "definition": "Definition 0.",
            "example_sentence": "Example 0.",
        }
    )
    context.commands["review"]("", source=root_command_source())

    cli_source = PluginCommandSource(
        authenticated=True,
        platform="cli",
        chat_id="7747352551",
        chat_type="dm",
        thread_id=None,
    )
    assert context.commands["endstudy"]("", source=cli_source) == SOURCE_ERROR

    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM study_sessions WHERE status = 'active'"
        ).fetchone()[0] == 1