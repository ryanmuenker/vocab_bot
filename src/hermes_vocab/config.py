from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAILY_NEW_CARD_LIMIT = 5
DEFAULT_REVIEW_HOUR = 12


class ConfigurationError(ValueError):
    """Raised when required local configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    timezone: ZoneInfo
    telegram_chat_id: int | None = None
    daily_new_card_limit: int = DAILY_NEW_CARD_LIMIT
    review_hour: int = DEFAULT_REVIEW_HOUR

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
        telegram_chat_id_text = os.environ.get(
            "HERMES_VOCAB_TELEGRAM_CHAT_ID", ""
        ).strip()
        try:
            telegram_chat_id = (
                int(telegram_chat_id_text) if telegram_chat_id_text else None
            )
        except ValueError as error:
            raise ConfigurationError(
                "HERMES_VOCAB_TELEGRAM_CHAT_ID must be a base-10 integer"
            ) from error
        review_hour_text = os.environ.get(
            "HERMES_VOCAB_REVIEW_HOUR",
            str(DEFAULT_REVIEW_HOUR),
        ).strip()
        try:
            review_hour = int(review_hour_text)
        except ValueError as error:
            raise ConfigurationError(
                "HERMES_VOCAB_REVIEW_HOUR must be an integer from 0 to 23"
            ) from error
        if not 0 <= review_hour <= 23:
            raise ConfigurationError(
                "HERMES_VOCAB_REVIEW_HOUR must be an integer from 0 to 23"
            )
        return cls(
            database_path=database_path,
            timezone=timezone,
            telegram_chat_id=telegram_chat_id,
            review_hour=review_hour,
        )
