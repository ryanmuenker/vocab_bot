from __future__ import annotations

from pathlib import Path

import pytest

from hermes_vocab.config import ConfigurationError, Settings


def test_settings_use_explicit_database_and_timezone(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "vocabulary.sqlite3"
    monkeypatch.setenv("HERMES_VOCAB_DB", str(path))
    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")

    settings = Settings.from_environment()

    assert settings.database_path == path
    assert settings.timezone.key == "America/New_York"


def test_invalid_timezone_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_TIMEZONE", "Not/A_Timezone")

    with pytest.raises(ConfigurationError):
        Settings.from_environment()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("7747352551", 7747352551), ("-100123", -100123)],
)
def test_optional_telegram_chat_id_parsing(
    monkeypatch,
    value: str | None,
    expected: int | None,
) -> None:
    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
    if value is None:
        monkeypatch.delenv("HERMES_VOCAB_TELEGRAM_CHAT_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_VOCAB_TELEGRAM_CHAT_ID", value)

    assert Settings.from_environment().telegram_chat_id == expected


def test_invalid_telegram_chat_id_names_variable(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
    monkeypatch.setenv("HERMES_VOCAB_TELEGRAM_CHAT_ID", "not-an-integer")

    with pytest.raises(ConfigurationError, match="HERMES_VOCAB_TELEGRAM_CHAT_ID"):
        Settings.from_environment()
