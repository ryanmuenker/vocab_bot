#!/usr/bin/env python3
from __future__ import annotations

from hermes_vocab.config import Settings
from hermes_vocab.database import Database
from hermes_vocab.formatting import format_daily_review
from hermes_vocab.review import ReviewService


def main() -> int:
    settings = Settings.from_environment()
    database = Database(settings.database_path)
    database.initialize()
    text = format_daily_review(
        ReviewService(database, settings.timezone).daily_review()
    )
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
