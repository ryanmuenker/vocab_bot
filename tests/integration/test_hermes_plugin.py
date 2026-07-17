from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hermes_vocab.capture import MAX_SOURCE_CONTEXT_LENGTH
from hermes_vocab.database import Database
from hermes_vocab.config import Settings
from hermes_vocab.formatting import format_daily_review
from hermes_vocab.hermes_plugin import register
from hermes_vocab.review import ReviewService


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.tool_schemas: dict[str, dict] = {}
        self.toolsets: dict[str, str] = {}
        self.hooks: dict[str, object] = {}
        self.skills: dict[str, Path] = {}

    def register_tool(self, *, name, toolset, schema, handler) -> None:
        self.tools[name] = handler
        self.tool_schemas[name] = schema
        self.toolsets[name] = toolset

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback

    def register_skill(self, name, path) -> None:
        self.skills[name] = Path(path)


def register_plugin(monkeypatch, tmp_path: Path) -> tuple[FakeContext, Path]:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    monkeypatch.setenv("HERMES_VOCAB_DB", str(path))
    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
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


def test_registration_exposes_two_tools_hook_and_skill(monkeypatch, tmp_path: Path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)

    assert set(context.tools) == {"vocabulary_save_card", "vocabulary_complete_review"}
    assert set(context.hooks) == {"pre_llm_call"}
    assert set(context.skills) == {"vocabulary"}
    assert context.skills["vocabulary"].name == "SKILL.md"
    assert context.toolsets == {
        "vocabulary_save_card": "vocabulary",
        "vocabulary_complete_review": "vocabulary",
    }
    save_schema = context.tool_schemas["vocabulary_save_card"]["parameters"]
    assert save_schema["required"] == ["display_text", "operation"]
    assert save_schema["properties"]["operation"] == {
        "type": "string",
        "enum": ["new_entry", "new_sense", "existing_sense"],
    }
    assert save_schema["properties"]["source_context"] == {"type": "string"}
    assert save_schema["properties"]["matching_sense_id"] == {"type": "integer"}


def test_telegram_single_word_injects_capture_guidance(monkeypatch, tmp_path: Path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    callback = context.hooks["pre_llm_call"]

    result = hook_call(callback, "obdurate")

    assert "vocabulary:vocabulary" in result
    assert "vocabulary_save_card" in result
    assert "vocabulary_save_card" in hook_call(callback, "what does this mean?")
    assert hook_call(callback, "obdurate", platform="cli") is None


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
        context.tools["vocabulary_complete_review"](
            {"answer_text": "brief and direct"}
        )
    )

    assert "vocabulary_complete_review" in guidance
    assert result["status"] == "completed"
    assert result["text"] == (
        "Definition:\nUsing very few words.\n\n"
        "Example:\nShe visited the bank."
    )
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

    completion = json.loads(
        restarted_context.tools["vocabulary_complete_review"](
            {"answer_text": answer_text}
        )
    )
    assert completion == {
        "status": "completed",
        "text": (
            "1. noun — A financial institution.\n"
            "   Example: She deposited the cheque at the bank.\n\n"
            "2. noun — Land alongside a river.\n"
            "   Example: They rested on the grassy bank."
        ),
    }
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