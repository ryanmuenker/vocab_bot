from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    """Raised when required local configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    timezone: ZoneInfo

    @classmethod
    def from_environment(cls) -> "Settings":
        database_path = Path(
            os.environ.get(
                "HERMES_VOCAB_DB",
                "~/.local/share/hermes-vocab/vocabulary.sqlite3",
            )
        ).expanduser()
        timezone_name = os.environ.get("HERMES_TIMEZONE", "").strip()
        if not timezone_name:
            raise ConfigurationError("HERMES_TIMEZONE must be an IANA timezone")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ConfigurationError(
                f"Invalid IANA timezone: {timezone_name}"
            ) from error
        return cls(database_path=database_path, timezone=timezone)
