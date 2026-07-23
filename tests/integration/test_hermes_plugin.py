from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock
from datetime import UTC, datetime
from pathlib import Path

from hermes_vocab.capture import MAX_SOURCE_CONTEXT_LENGTH
from hermes_vocab.database import Database
from hermes_vocab.config import Settings
from hermes_vocab.formatting import format_daily_review, format_entry
from hermes_vocab.hermes_plugin import register
from hermes_vocab.models import ReviewPromptStatus
from hermes_vocab.review import PendingReviewStatus, ReviewService


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
    ) -> None:
        self.commands[name] = handler
        self.command_descriptions[name] = description
        self.command_args_hints[name] = args_hint

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


def register_plugin(
    monkeypatch,
    tmp_path: Path,
    *,
    chat_id: int | None = None,
) -> tuple[FakeContext, Path]:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    monkeypatch.setenv("HERMES_VOCAB_DB", str(path))
    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
    if chat_id is None:
        monkeypatch.delenv("HERMES_VOCAB_TELEGRAM_CHAT_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_VOCAB_TELEGRAM_CHAT_ID", str(chat_id))
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


def test_registration_exposes_tools_hook_skill_and_auxiliary_task(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)

    assert set(context.tools) == {"vocabulary_save_card", "vocabulary_complete_review"}
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
        "vocabulary_complete_review": "vocabulary",
    }
    assert context.tool_is_async == {
        "vocabulary_save_card": False,
        "vocabulary_complete_review": True,
    }
    assert "Do not grade" not in context.tool_schemas[
        "vocabulary_complete_review"
    ]["description"]
    save_schema = context.tool_schemas["vocabulary_save_card"]["parameters"]
    assert save_schema["required"] == ["display_text", "operation"]
    assert save_schema["properties"]["operation"] == {
        "type": "string",
        "enum": ["new_entry", "new_sense", "existing_sense"],
    }
    assert save_schema["properties"]["source_context"] == {"type": "string"}
    assert save_schema["properties"]["matching_sense_id"] == {"type": "integer"}


def test_configured_chat_registers_supported_parameterless_test_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)

    assert set(context.commands) == {"test"}
    assert context.command_descriptions == {
        "test": "Start or resume a five-word vocabulary test"
    }
    assert context.command_args_hints == {"test": ""}
    assert set(context.hooks) == {"pre_llm_call", "gateway_inbound_intercept"}


def test_test_command_rejects_arguments_and_insufficient_library_without_rows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    for index in range(4):
        context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text=f"word-{index}",
                definition=f"Definition {index}.",
            )
        )

    assert context.commands["test"]("restart") == "Usage: /test"
    assert context.commands["test"]("") == (
        "You have 4 saved entries. Save 1 more to start a 5-word test."
    )
    with Database(path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM test_questions").fetchone()[0] == 0


def test_test_command_starts_resumes_and_obeys_daily_review_conflict(
    monkeypatch,
    tmp_path: Path,
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

    first = context.commands["test"]("")
    duplicate = context.commands["test"]("   ")

    assert first == "Question 1 of 5\nWhat does 'word-0' mean?"
    assert duplicate == first
    with Database(path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM test_questions").fetchone()[0] == 5

    other_context, other_path = register_plugin(
        monkeypatch,
        tmp_path / "pending",
        chat_id=7747352551,
    )
    for index in range(5):
        other_context.tools["vocabulary_save_card"](
            save_args(
                "new_entry",
                display_text=f"pending-{index}",
                definition=f"Definition {index}.",
            )
        )
    ReviewService(
        Database(other_path),
        Settings.from_environment().timezone,
        clock=lambda: datetime(2026, 7, 19, tzinfo=UTC),
    ).daily_review()

    assert other_context.commands["test"]("") == (
        "Finish your daily review before starting a test."
    )


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
    assert response == GatewayInterceptResponse(format_entry(entry, "Already saved."))
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


def test_contextual_hook_fails_open_when_review_state_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    callback = context.hooks["pre_llm_call"]
    monkeypatch.setattr(
        callback.__self__.review_service,
        "pending_review_status",
        lambda: PendingReviewStatus.STORAGE_ERROR,
    )

    assert hook_call(callback, "perfidy") is None


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


def test_pending_review_routes_next_message_and_completion_tool(monkeypatch, tmp_path: Path) -> None:
    call_llm = install_auxiliary_client(
        monkeypatch,
        '{"grade":"correct","feedback":"Accurate paraphrase."}',
    )
    context, _ = register_plugin(monkeypatch, tmp_path)
    save = context.tools["vocabulary_save_card"]
    save(
        save_args(
            "new_entry",
            display_text="laconic",
            definition="Using very few words.",
        )
    )

    settings = Settings.from_environment()
    ReviewService(
        Database(settings.database_path),
        settings.timezone,
        clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC),
    ).daily_review()

    callback = context.hooks["pre_llm_call"]
    guidance = hook_call(callback, "brief and direct")
    result = json.loads(
        asyncio.run(
            context.tools["vocabulary_complete_review"](
                {"answer_text": "brief and direct"}
            )
        )
    )

    assert "vocabulary_complete_review" in guidance
    assert "do not grade" not in guidance.lower()
    assert result["status"] == "completed"
    assert result["text"] == (
        "Grade: Correct\n"
        "Feedback: Accurate paraphrase.\n\n"
        "Definition:\nUsing very few words.\n\n"
        "Example:\nShe visited the bank."
    )
    call_llm.assert_awaited_once()
    assert hook_call(callback, "/help") is None


def test_pending_review_precedes_contextual_capture(
    monkeypatch, tmp_path: Path
) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    context.tools["vocabulary_save_card"](save_args("new_entry"))
    settings = Settings.from_environment()
    ReviewService(
        Database(path),
        settings.timezone,
        clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC),
    ).daily_review()

    guidance = hook_call(
        context.hooks["pre_llm_call"],
        "shore\nThey walked along the shore.",
    )

    assert "vocabulary_complete_review" in guidance
    assert "vocabulary_save_card" not in guidance


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


def test_multi_sense_capture_review_survives_restart(
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

    review_time = datetime(2026, 7, 16, 8, tzinfo=UTC)
    review_service = ReviewService(
        Database(path),
        Settings.from_environment().timezone,
        clock=lambda: review_time,
    )
    assert format_daily_review(review_service.daily_review()) == (
        "What does 'bank' mean?"
    )

    restarted_context, restarted_path = register_plugin(monkeypatch, tmp_path)
    assert restarted_path == path
    answer_text = "A place for money, or the edge of a river."
    restarted_routing = hook_call(
        restarted_context.hooks["pre_llm_call"],
        answer_text,
    )
    assert "vocabulary_complete_review" in restarted_routing
    assert "vocabulary_save_card" not in restarted_routing

    call_llm = install_auxiliary_client(
        monkeypatch,
        '{"grade":"correct","feedback":"You covered both senses."}',
    )
    completion = json.loads(
        asyncio.run(
            restarted_context.tools["vocabulary_complete_review"](
                {"answer_text": answer_text}
            )
        )
    )
    assert completion == {
        "status": "completed",
        "text": (
            "Grade: Correct\n"
            "Feedback: You covered both senses.\n\n"
            "1. noun — A financial institution.\n"
            "   Example: She deposited the cheque at the bank.\n\n"
            "2. noun — Land alongside a river.\n"
            "   Example: They rested on the grassy bank."
        ),
    }
    payload = json.loads(call_llm.await_args.kwargs["messages"][1]["content"])
    assert [sense["definition"] for sense in payload["senses"]] == [
        "A financial institution.",
        "Land alongside a river.",
    ]
    assert format_daily_review(review_service.daily_review()) == ""

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
                (SELECT COUNT(*) FROM vocabulary_senses),
                (SELECT COUNT(*) FROM review_events WHERE status = 'answered')
            """
        ).fetchone()
        event = connection.execute(
            "SELECT status, answer_text FROM review_events"
        ).fetchone()

    assert tuple(reopened_counts) == (1, 2, 1)
    assert tuple(event) == ("answered", answer_text)


def test_dedicated_review_route_awaits_shared_semantic_evaluation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    @dataclass(frozen=True, slots=True)
    class GatewayInterceptResponse:
        text: str

    call_llm = install_auxiliary_client(
        monkeypatch,
        '{"grade":"partial","feedback":"You identified the general idea."}',
    )
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.GatewayInterceptResponse = GatewayInterceptResponse
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    context, path = register_plugin(monkeypatch, tmp_path, chat_id=7747352551)
    context.tools["vocabulary_save_card"](
        save_args(
            "new_entry",
            display_text="laconic",
            definition="Using very few words.",
        )
    )
    settings = Settings.from_environment()
    pending = ReviewService(
        Database(path),
        settings.timezone,
        clock=lambda: datetime.now(UTC),
    ).daily_review()
    assert pending.event is not None

    response = asyncio.run(
        context.hooks["gateway_inbound_intercept"](
            platform="telegram",
            sender_id="42",
            chat_id="7747352551",
            chat_type="dm",
            thread_id=None,
            user_message="It means being brief.",
        )
    )

    assert response == GatewayInterceptResponse(
        "Grade: Partial\n"
        "Feedback: You identified the general idea.\n\n"
        "Definition:\nUsing very few words.\n\n"
        "Example:\nShe visited the bank."
    )
    call_llm.assert_awaited_once()
    assert call_llm.await_args.kwargs["task"] == "vocabulary_answer_evaluation"
    with Database(path).connect() as connection:
        event = connection.execute(
            "SELECT status, answer_text, grade, evaluation_feedback FROM review_events"
        ).fetchone()
    assert tuple(event) == (
        "answered",
        "It means being brief.",
        "partial",
        "You identified the general idea.",
    )


def test_async_tool_show_answer_bypasses_auxiliary_provider(
    monkeypatch,
    tmp_path: Path,
) -> None:
    call_llm = install_auxiliary_client(
        monkeypatch,
        '{"grade":"correct","feedback":"Should not be used."}',
    )
    context, path = register_plugin(monkeypatch, tmp_path)
    context.tools["vocabulary_save_card"](
        save_args(
            "new_entry",
            display_text="laconic",
            definition="Using very few words.",
        )
    )
    settings = Settings.from_environment()
    ReviewService(
        Database(path),
        settings.timezone,
        clock=lambda: datetime.now(UTC),
    ).daily_review()

    payload = json.loads(
        asyncio.run(
            context.tools["vocabulary_complete_review"](
                {"answer_text": "show answer"}
            )
        )
    )

    assert payload == {
        "status": "completed",
        "text": (
            "Grade: Incorrect\n"
            "Feedback: You chose to reveal the answer.\n\n"
            "Definition:\nUsing very few words.\n\n"
            "Example:\nShe visited the bank."
        ),
    }
    call_llm.assert_not_awaited()


def test_registered_test_cross_path_survives_restart_and_evaluator_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    @dataclass(frozen=True, slots=True)
    class GatewayInterceptResponse:
        text: str

    call_llm = install_auxiliary_client(monkeypatch, "")
    call_llm.side_effect = [
        '{"grade":"correct","feedback":"Feedback 1."}',
        RuntimeError("provider unavailable"),
        '{"grade":"partial","feedback":"Feedback 2."}',
        '{"grade":"incorrect","feedback":"Feedback 3."}',
        '{"grade":"correct","feedback":"Feedback 4."}',
        '{"grade":"partial","feedback":"Feedback 5."}',
    ]
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.GatewayInterceptResponse = GatewayInterceptResponse
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

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
        scheduling_before_test = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, last_reviewed, review_status
                FROM vocabulary_entries
                ORDER BY id
                """
            )
        ]
    assert context.commands["test"]("") == (
        "Question 1 of 5\nWhat does 'word-0' mean?"
    )

    async def answer(active_context: FakeContext, text: str) -> str:
        response = await active_context.hooks["gateway_inbound_intercept"](
            platform="telegram",
            sender_id="42",
            chat_id="7747352551",
            chat_type="dm",
            thread_id=None,
            user_message=text,
        )
        assert isinstance(response, GatewayInterceptResponse)
        return response.text

    first = asyncio.run(answer(context, "answer 1"))
    assert first.endswith("Question 2 of 5\nWhat does 'word-1' mean?")

    restarted = FakeContext()
    register(restarted)
    morning = ReviewService(
        Database(path),
        Settings.from_environment().timezone,
        clock=lambda: datetime(2026, 7, 19, 8, tzinfo=UTC),
    ).daily_review()
    assert morning.status is ReviewPromptStatus.TEST_ACTIVE
    assert format_daily_review(morning) == ""
    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_events"
        ).fetchone()[0] == 0
    assert restarted.commands["test"]("") == (
        "Question 2 of 5\nWhat does 'word-1' mean?"
    )

    failed = asyncio.run(answer(restarted, "discarded attempt"))
    assert failed == "I couldn't evaluate that answer. Please try again."
    assert "Definition 1." not in failed
    assert restarted.commands["test"]("") == (
        "Question 2 of 5\nWhat does 'word-1' mean?"
    )
    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM test_questions WHERE answer_text IS NOT NULL"
        ).fetchone()[0] == 1

    second = asyncio.run(answer(restarted, "answer 2"))
    third = asyncio.run(answer(restarted, "answer 3"))
    fourth = asyncio.run(answer(restarted, "answer 4"))
    fifth = asyncio.run(answer(restarted, "answer 5"))

    assert second.endswith("Question 3 of 5\nWhat does 'word-2' mean?")
    assert third.endswith("Question 4 of 5\nWhat does 'word-3' mean?")
    assert fourth.endswith("Question 5 of 5\nWhat does 'word-4' mean?")
    assert fifth.endswith(
        "Test complete.\n"
        "Results: 2 correct, 2 partial, 1 incorrect."
    )
    assert [call.kwargs["task"] for call in call_llm.await_args_list] == [
        "vocabulary_answer_evaluation"
    ] * 6
    with Database(path).connect() as connection:
        rows = connection.execute(
            """
            SELECT position, answer_text, grade
            FROM test_questions ORDER BY position
            """
        ).fetchall()
        session = connection.execute(
            "SELECT status FROM test_sessions"
        ).fetchone()[0]
        scheduling_after_test = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, last_reviewed, review_status
                FROM vocabulary_entries
                ORDER BY id
                """
            )
        ]
    assert [tuple(row) for row in rows] == [
        (1, "answer 1", "correct"),
        (2, "answer 2", "partial"),
        (3, "answer 3", "incorrect"),
        (4, "answer 4", "correct"),
        (5, "answer 5", "partial"),
    ]
    assert session == "completed"
    assert scheduling_after_test == scheduling_before_test

    fallthrough = asyncio.run(answer(restarted, "word-0"))
    assert fallthrough.endswith("Already saved.")
    assert call_llm.await_count == 6