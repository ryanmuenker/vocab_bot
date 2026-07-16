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
