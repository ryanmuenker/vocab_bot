from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hermes_vocab.database import Database
from hermes_vocab.hermes_plugin import register


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.hooks: dict[str, object] = {}
        self.skills: dict[str, Path] = {}

    def register_tool(self, *, name, toolset, schema, handler) -> None:
        self.tools[name] = handler

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


def test_registration_exposes_two_tools_hook_and_skill(monkeypatch, tmp_path: Path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)

    assert set(context.tools) == {"vocabulary_save_card", "vocabulary_complete_review"}
    assert set(context.hooks) == {"pre_llm_call"}
    assert set(context.skills) == {"vocabulary"}
    assert context.skills["vocabulary"].name == "SKILL.md"


def test_telegram_single_word_injects_capture_guidance(monkeypatch, tmp_path: Path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    callback = context.hooks["pre_llm_call"]

    result = hook_call(callback, "obdurate")

    assert "vocabulary:vocabulary" in result
    assert "vocabulary_save_card" in result
    assert hook_call(callback, "what does this mean?") is None
    assert hook_call(callback, "obdurate", platform="cli") is None


def test_save_tool_returns_exact_formatted_text_and_persists(monkeypatch, tmp_path: Path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)

    payload = json.loads(
        context.tools["vocabulary_save_card"](
            {
                "word": "obdurate",
                "part_of_speech": "adjective",
                "definition": "Stubbornly refusing to change one's opinion.",
                "example_sentence": "The committee remained obdurate despite new evidence.",
            }
        )
    )

    assert payload["status"] == "saved"
    assert payload["text"].endswith("✓ Saved.")
    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entries"
        ).fetchone()[0] == 1


def test_pending_review_routes_next_message_and_completion_tool(monkeypatch, tmp_path: Path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    save = context.tools["vocabulary_save_card"]
    save(
        {
            "word": "laconic",
            "part_of_speech": "adjective",
            "definition": "Using very few words.",
            "example_sentence": "His laconic reply ended the discussion.",
        }
    )

    from hermes_vocab.config import Settings
    from hermes_vocab.review import ReviewService

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
        "Example:\nHis laconic reply ended the discussion."
    )
    assert hook_call(callback, "/help") is None
