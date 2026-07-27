from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from hermes_vocab.capture import CaptureService, _timestamp
from hermes_vocab.database import Database
from hermes_vocab.hermes_plugin.hooks import VocabularyHook
from hermes_vocab.models import (
    CaptureCommand,
    CaptureOperation,
    SenseCard,
)
from hermes_vocab.migrations.v005_backfill import backfill_v5
from hermes_vocab.review import ReviewService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
CHAT_ID = "7747352551"
PROMPT_PATTERN = re.compile(
    r"^Review (\d+) of (\d+) · (\d+) due\nWhat does '(.+)' mean\?\n$"
)


def seed_cards(path: Path, words: Sequence[str]) -> Database:
    """Capture one forward card per word and project v5 scheduling state."""
    database = Database(path)
    database.initialize()
    capture = CaptureService(database)
    for word in words:
        capture.capture(
            CaptureCommand(
                display_text=word,
                operation=CaptureOperation.NEW_ENTRY,
                card=SenseCard(
                    part_of_speech="adjective",
                    definition=f"The quality of being {word}.",
                    example_sentence=f"His {word} reply ended the discussion.",
                ),
            )
        )
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        backfill_v5(connection)
        connection.commit()
    return database


def make_overdue(database: Database, words: Sequence[str], due_at: str) -> None:
    with database.connect() as connection:
        for word in words:
            connection.execute(
                """
                UPDATE vocabulary_cards
                SET state = 'review', stability = 2.0, difficulty = 5.0,
                    due_at = ?, effective_due_at = ?, last_review_at = ?,
                    repetitions = 1, lapses = 0
                WHERE direction = 'forward' AND entry_id = (
                    SELECT id FROM vocabulary_entries WHERE display_text = ?
                )
                """,
                (due_at, due_at, due_at, word),
            )
        connection.commit()


def fixed_offset_timezone(local_hour: int) -> str:
    """Return an IANA fixed-offset zone whose current local hour is exact."""
    offset = (local_hour - datetime.now(UTC).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


def run_cron(
    path: Path,
    *,
    run_id: str | None = "cron-run-1",
    review_hour: str = "0",
    timezone: str = "UTC",
    chat_id: str | None = CHAT_ID,
    python_path: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "HERMES_VOCAB_DB": str(path),
        "HERMES_TIMEZONE": timezone,
        "HERMES_VOCAB_REVIEW_HOUR": review_hour,
        "PYTHONPATH": os.pathsep.join(
            (
                *python_path,
                str(PROJECT_ROOT / "src"),
                str(HERMES_ROOT),
                os.environ.get("PYTHONPATH", ""),
            )
        ),
    }
    if chat_id is None:
        environment.pop("HERMES_VOCAB_TELEGRAM_CHAT_ID", None)
    else:
        environment["HERMES_VOCAB_TELEGRAM_CHAT_ID"] = chat_id
    if run_id is None:
        environment.pop("HERMES_CRON_RUN_ID", None)
    else:
        environment["HERMES_CRON_RUN_ID"] = run_id
    return subprocess.run(
        [sys.executable, "scripts/daily_review.py"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def study_state_counts(database: Database) -> tuple[int, int, int]:
    with database.connect() as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("study_sessions", "study_prompts", "study_queue")
        )


def test_cron_prepares_one_correlated_prompt_without_marking_it_delivered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = seed_cards(path, ["laconic"])

    completed = run_cron(path)

    assert completed.returncode == 0
    assert completed.stdout.startswith("Review 1 of ")
    assert completed.stdout.endswith("What does 'laconic' mean?\n")
    assert completed.stderr == ""
    with database.connect() as connection:
        prompt = connection.execute(
            "SELECT id, status FROM study_prompts"
        ).fetchone()
        assert prompt is not None and prompt["status"] == "prepared"
        attempt = connection.execute(
            """
            SELECT status, outbound_delivery_id
            FROM prompt_delivery_attempts WHERE prompt_id = ?
            """,
            (prompt["id"],),
        ).fetchone()
        assert tuple(attempt) == ("unknown", "cron-run-1")


def test_cron_backlog_emits_one_counted_prompt_then_stays_silent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    overdue = ["laconic", "perfidy", "obdurate"]
    database = seed_cards(path, [*overdue, *(f"word-{index}" for index in range(5))])
    make_overdue(database, overdue, "2026-01-05T12:00:00Z")

    first = run_cron(path, run_id="cron-run-1")

    assert first.returncode == 0
    assert first.stderr == ""
    match = PROMPT_PATTERN.match(first.stdout)
    assert match is not None, first.stdout
    assert (match.group(1), match.group(2), match.group(3)) == ("1", "8", "3")
    assert match.group(4) in overdue

    second = run_cron(path, run_id="cron-run-2")

    assert second.returncode == 0
    assert second.stdout == ""
    assert second.stderr == ""
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*), MIN(status) FROM study_prompts"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM study_prompts"
        ).fetchone()[0] == "prepared"
        assert connection.execute(
            "SELECT COUNT(*) FROM prompt_delivery_attempts"
        ).fetchone()[0] == 1


def test_cron_before_review_hour_writes_nothing_without_older_backlog(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = seed_cards(path, [f"word-{index}" for index in range(5)])

    completed = run_cron(
        path,
        review_hour="12",
        timezone=fixed_offset_timezone(3),
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert study_state_counts(database) == (0, 0, 0)
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM prompt_delivery_attempts"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_cards WHERE introduced_local_date IS NOT NULL"
        ).fetchone()[0] == 0


def test_cron_before_review_hour_catches_up_on_older_backlog(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = seed_cards(
        path,
        ["laconic", *(f"word-{index}" for index in range(5))],
    )
    make_overdue(database, ["laconic"], "2020-01-01T00:00:00Z")

    completed = run_cron(
        path,
        review_hour="12",
        timezone=fixed_offset_timezone(3),
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    match = PROMPT_PATTERN.match(completed.stdout)
    assert match is not None, completed.stdout
    assert match.group(1) == "1"
    assert match.group(4) == "laconic"
    sessions, prompts, _ = study_state_counts(database)
    assert (sessions, prompts) == (1, 1)


def incompatible_hermes(tmp_path: Path) -> str:
    """Return a sys.path root whose hermes_cli lacks the receipt contract."""
    package = tmp_path / "incompatible" / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "plugin_contracts.py").write_text(
        "OutboundResponse = object\nPluginCommandSource = object\n",
        encoding="utf-8",
    )
    return str(package.parent)


def test_cron_fails_closed_when_hermes_lacks_the_receipt_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = seed_cards(path, ["laconic"])

    completed = run_cron(path, python_path=(incompatible_hermes(tmp_path),))

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("Vocabulary cron configuration error:")
    assert "OutboundDeliveryReceipt" in completed.stderr
    assert study_state_counts(database) == (0, 0, 0)


def test_cron_fails_closed_without_hermes_run_correlation(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = seed_cards(path, ["laconic"])

    completed = run_cron(path, run_id=None)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == (
        "Vocabulary cron configuration error: Delivery-safe cron requires "
        "Telegram chat and Hermes run identity"
    )
    assert study_state_counts(database) == (0, 0, 0)


def test_cron_retries_the_same_prompt_after_a_failed_delivery_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = seed_cards(path, ["laconic"])

    first = run_cron(path, run_id="cron-run-1")

    assert PROMPT_PATTERN.match(first.stdout) is not None, first.stdout
    prompt_text = first.stdout.rstrip("\n")
    review = ReviewService(database, ZoneInfo("UTC"))
    hook = VocabularyHook(CaptureService(database), review, int(CHAT_ID))
    hook.post_outbound_delivery(
        receipt=SimpleNamespace(
            state="failure",
            destination=f"telegram:{CHAT_ID}",
            message_ids=(),
            correlation_id=None,
            cron_run_id="cron-run-1",
            content_fingerprint=sha256(prompt_text.encode()).hexdigest(),
            error="transport unavailable",
        )
    )
    assert review.answerable_prompt() is None

    second = run_cron(path, run_id="cron-run-2")

    assert second.returncode == 0
    assert second.stdout == first.stdout
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM study_prompts"
        ).fetchone()[0] == 1
        attempts = connection.execute(
            """
            SELECT status, outbound_delivery_id
            FROM prompt_delivery_attempts ORDER BY id
            """
        ).fetchall()
    assert [tuple(row) for row in attempts] == [
        ("unknown", "cron-run-1"),
        ("failed", "cron-run-1"),
        ("unknown", "cron-run-2"),
    ]


def test_cron_recovers_when_a_delivery_never_reported_a_receipt(
    tmp_path: Path,
) -> None:
    """A gateway killed mid-send must not silence the ticker forever."""
    path = tmp_path / "data" / "vocabulary.sqlite3"
    database = seed_cards(path, ["laconic"])

    first = run_cron(path, run_id="cron-run-1")
    assert PROMPT_PATTERN.match(first.stdout) is not None, first.stdout

    # No receipt ever arrives: the process died between prepare and callback.
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM prompt_delivery_attempts WHERE receipt_at IS NULL"
        ).fetchone()[0] == 1

    # A tick while the send is genuinely in flight stays silent.
    assert run_cron(path, run_id="cron-run-2").stdout == ""

    # Once the attempt is older than the staleness bound it is abandoned, and the
    # same prompt identity is retried rather than the queue going quiet forever.
    # Attempts are append-only by trigger, so age the row with the guard lifted:
    # this stands in for 30 minutes passing, not for a write the product makes.
    with database.connect() as connection:
        connection.execute("DROP TRIGGER prompt_delivery_attempts_immutable_update")
        connection.execute(
            "UPDATE prompt_delivery_attempts"
            " SET attempted_at = ?"
            " WHERE receipt_at IS NULL",
            (_timestamp(datetime.now(UTC) - timedelta(minutes=30)),),
        )
        connection.execute(
            """
            CREATE TRIGGER prompt_delivery_attempts_immutable_update
            BEFORE UPDATE ON prompt_delivery_attempts
            BEGIN
                SELECT RAISE(ABORT, 'delivery attempts are immutable');
            END
            """
        )
        connection.commit()

    recovered = run_cron(path, run_id="cron-run-3")

    assert recovered.returncode == 0
    assert recovered.stdout == first.stdout
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM study_prompts"
        ).fetchone()[0] == 1
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT status, outbound_delivery_id"
                " FROM prompt_delivery_attempts ORDER BY id"
            )
        ] == [("unknown", "cron-run-1"), ("unknown", "cron-run-3")]
