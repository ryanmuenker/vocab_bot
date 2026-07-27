#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from hermes_vocab.capture import CaptureService, _timestamp
from hermes_vocab.config import ConfigurationError, Settings
from hermes_vocab.database import Database
from hermes_vocab.hermes_plugin.gateway import VocabularyGatewayRouter
from hermes_vocab.hermes_plugin.hooks import VocabularyHook
from hermes_vocab.models import StudyMode, StudyStartStatus
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService


# A prepared send with no receipt is treated as abandoned after this long, so a
# crash between preparing a prompt and recording its receipt cannot wedge cron.
ABANDONED_DELIVERY_AFTER = timedelta(minutes=5)


def main() -> int:
    try:
        from hermes_cli.plugin_contracts import (
            OutboundDeliveryReceipt,
            OutboundResponse,
            PluginCommandSource,
        )

        if any(
            contract is None
            for contract in (
                OutboundDeliveryReceipt,
                OutboundResponse,
                PluginCommandSource,
            )
        ):
            raise ImportError
        settings = Settings.from_environment()
        cron_run_id = os.environ.get("HERMES_CRON_RUN_ID", "").strip()
        if settings.telegram_chat_id is None or not cron_run_id:
            raise ConfigurationError(
                "Delivery-safe cron requires Telegram chat and Hermes run identity"
            )
    except (ConfigurationError, ImportError, AttributeError) as error:
        print(f"Vocabulary cron configuration error: {error}", file=sys.stderr)
        return 2

    database = Database(settings.database_path)
    database.initialize()
    review = ReviewService(database, settings.timezone)
    test_service = TestSessionService(database, settings.timezone)
    if (
        review.answerable_prompt() is not None
        or review.awaiting_rating() is not None
        or review.active_mode() in {StudyMode.TEST_FORWARD, StudyMode.TEST_REVERSE}
    ):
        return 0

    now = datetime.now(settings.timezone)
    with database.connect() as connection:
        # A send is only "in flight" while its attempt is recent. Without this
        # bound a gateway killed between prepare and receipt would leave the
        # prompt pending forever and silence the ticker for good. attempted_at is
        # written by SQLite's datetime('now'), so the bound is computed the same
        # way; an ISO-8601 string would not compare against it.
        in_flight = connection.execute(
            """
            SELECT 1 FROM study_prompts p
            JOIN study_sessions s ON s.id = p.session_id
            JOIN study_queue q ON q.id = p.queue_item_id
            JOIN prompt_delivery_attempts a ON a.id = (
                SELECT MAX(id) FROM prompt_delivery_attempts
                WHERE prompt_id = p.id
            )
            WHERE s.status = 'active' AND q.status = 'current'
              AND p.status = 'prepared' AND a.receipt_at IS NULL
              AND a.attempted_at > datetime('now', ?)
            LIMIT 1
            """,
            (f"-{int(ABANDONED_DELIVERY_AFTER.total_seconds())} seconds",),
        ).fetchone()
        older_backlog = connection.execute(
            """
            SELECT 1 FROM vocabulary_cards
            WHERE state != 'new'
              AND substr(effective_due_at, 1, 10) < ?
              AND (buried_until_local_date IS NULL
                   OR buried_until_local_date < ?)
            LIMIT 1
            """,
            (now.date().isoformat(), now.date().isoformat()),
        ).fetchone()
    if in_flight is not None:
        return 0
    if now.hour < settings.review_hour and older_backlog is None:
        return 0

    started = review.start()
    if started.status not in {
        StudyStartStatus.STARTED,
        StudyStartStatus.RESUMED,
    }:
        return 0
    hook = VocabularyHook(
        CaptureService(database),
        review,
        settings.telegram_chat_id,
    )
    router = VocabularyGatewayRouter(
        hook.capture_service,
        review,
        test_service,
        None,
        None,
        settings.telegram_chat_id,
        hook,
    )
    prompt = router.prepare_review_prompt()
    if prompt is None:
        return 0
    if not hook.prepare_outbound(
        prompt_id=prompt.id,
        identity=cron_run_id,
        text=prompt.prompt_text,
    ):
        return 1
    print(prompt.prompt_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
