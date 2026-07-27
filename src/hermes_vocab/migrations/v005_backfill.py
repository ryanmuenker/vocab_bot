from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from hermes_vocab.models import CardSchedule, CardScheduleState, ReviewRating
from hermes_vocab.scheduling import (
    DESIRED_RETENTION,
    PARAMETER_FINGERPRINT,
    PARAMETERS_VERSION,
    SCHEDULER_KIND,
    SCHEDULER_VERSION,
    transition,
)

_VALID_GRADES = {"correct", "partial", "incorrect"}


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        return None
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _create_cards(connection: sqlite3.Connection) -> None:
    entries = connection.execute(
        """
        SELECT e.id, e.date_added FROM vocabulary_entries e
        WHERE NOT EXISTS (
            SELECT 1 FROM vocabulary_cards c
            WHERE c.entry_id = e.id AND c.direction = 'forward'
        )
        ORDER BY e.id
        """
    ).fetchall()
    for entry in entries:
        connection.execute(
            """
            INSERT INTO vocabulary_cards (
                entry_id, sense_id, direction, state, due_at, effective_due_at,
                repetitions, lapses, scheduler_kind, scheduler_version,
                parameters_version, parameter_fingerprint, desired_retention,
                created_at
            ) VALUES (?, NULL, 'forward', 'new', ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["id"],
                entry["date_added"],
                entry["date_added"],
                SCHEDULER_KIND,
                SCHEDULER_VERSION,
                PARAMETERS_VERSION,
                PARAMETER_FINGERPRINT,
                DESIRED_RETENTION,
                entry["date_added"],
            ),
        )

    senses = connection.execute(
        """
        SELECT s.id, s.entry_id, s.date_added FROM vocabulary_senses s
        WHERE NOT EXISTS (
            SELECT 1 FROM vocabulary_cards c
            WHERE c.sense_id = s.id AND c.direction = 'reverse'
        )
        ORDER BY s.id
        """
    ).fetchall()
    for sense in senses:
        connection.execute(
            """
            INSERT INTO vocabulary_cards (
                entry_id, sense_id, direction, state, due_at, effective_due_at,
                repetitions, lapses, scheduler_kind, scheduler_version,
                parameters_version, parameter_fingerprint, desired_retention,
                created_at
            ) VALUES (?, ?, 'reverse', 'new', ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                sense["entry_id"],
                sense["id"],
                sense["date_added"],
                sense["date_added"],
                SCHEDULER_KIND,
                SCHEDULER_VERSION,
                PARAMETERS_VERSION,
                PARAMETER_FINGERPRINT,
                DESIRED_RETENTION,
                sense["date_added"],
            ),
        )


def _history(connection: sqlite3.Connection) -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    for row in connection.execute(
        """
        SELECT id, entry_id, answered_at, answer_text, grade,
               evaluation_feedback
        FROM review_events
        WHERE status = 'answered' AND answered_at IS NOT NULL
              AND grade IS NOT NULL
        """
    ):
        reviewed_at = _parse_utc(row["answered_at"])
        if reviewed_at is None or row["grade"] not in _VALID_GRADES:
            continue
        history.append(
            {
                "source": "review_event",
                "id": row["id"],
                "entry_id": row["entry_id"],
                "reviewed_at": reviewed_at,
                "answer": row["answer_text"],
                "grade": row["grade"],
                "feedback": row["evaluation_feedback"],
            }
        )

    for row in connection.execute(
        """
        SELECT id, entry_id, answered_at, answer_text, grade,
               evaluation_feedback
        FROM test_questions
        WHERE answered_at IS NOT NULL AND grade IS NOT NULL
        """
    ):
        reviewed_at = _parse_utc(row["answered_at"])
        if reviewed_at is None or row["grade"] not in _VALID_GRADES:
            continue
        history.append(
            {
                "source": "test_question",
                "id": row["id"],
                "entry_id": row["entry_id"],
                "reviewed_at": reviewed_at,
                "answer": row["answer_text"],
                "grade": row["grade"],
                "feedback": row["evaluation_feedback"],
            }
        )

    history.sort(key=lambda item: (item["reviewed_at"], item["source"], item["id"]))
    return history


def _schedule_from_row(row: sqlite3.Row) -> CardSchedule:
    return CardSchedule(
        state=CardScheduleState(row["state"]),
        stability=row["stability"],
        difficulty=row["difficulty"],
        due=_parse_required_utc(row["due_at"]),
        last_review=_parse_utc(row["last_review_at"]),
        repetitions=row["repetitions"],
        lapses=row["lapses"],
        scheduler_kind=row["scheduler_kind"],
        scheduler_version=row["scheduler_version"],
        parameters_version=row["parameters_version"],
        parameter_fingerprint=row["parameter_fingerprint"],
        desired_retention=row["desired_retention"],
    )


def _parse_required_utc(value: str) -> datetime:
    parsed = _parse_utc(value)
    if parsed is None:
        raise ValueError(f"invalid UTC timestamp in v4 data: {value!r}")
    return parsed


def _replay_history(connection: sqlite3.Connection) -> None:
    for item in _history(connection):
        card = connection.execute(
            """
            SELECT * FROM vocabulary_cards
            WHERE entry_id = ? AND direction = 'forward'
            """,
            (item["entry_id"],),
        ).fetchone()
        before = _schedule_from_row(card)
        rating = (
            ReviewRating.GOOD
            if item["grade"] == "correct"
            else ReviewRating.AGAIN
        )
        result = transition(before, rating, item["reviewed_at"])
        reviewed_at = _timestamp(result.reviewed_at)
        raw_due = _timestamp(result.raw_due)
        before_effective_due = card["effective_due_at"]
        connection.execute(
            """
            INSERT INTO review_attempts (
                card_id, source, rating, submitted_answer, evaluator_grade,
                evaluation_feedback, reviewed_at,
                before_state, before_stability, before_difficulty,
                before_due_at, before_effective_due_at, before_last_review_at,
                before_repetitions, before_lapses,
                after_state, after_stability, after_difficulty,
                after_raw_due_at, after_effective_due_at, after_last_review_at,
                after_repetitions, after_lapses,
                scheduler_kind, scheduler_version, parameters_version,
                parameter_fingerprint, desired_retention,
                legacy_source, legacy_id, created_at
            ) VALUES (
                ?, 'migration', ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                card["id"],
                rating.value,
                item["answer"],
                item["grade"],
                item["feedback"],
                reviewed_at,
                before.state.value,
                before.stability,
                before.difficulty,
                _timestamp(before.due),
                before_effective_due,
                _timestamp(before.last_review) if before.last_review else None,
                before.repetitions,
                before.lapses,
                result.after.state.value,
                result.after.stability,
                result.after.difficulty,
                raw_due,
                raw_due,
                reviewed_at,
                result.after.repetitions,
                result.after.lapses,
                result.after.scheduler_kind,
                result.after.scheduler_version,
                result.after.parameters_version,
                result.after.parameter_fingerprint,
                result.after.desired_retention,
                item["source"],
                item["id"],
                reviewed_at,
            ),
        )
        connection.execute(
            """
            UPDATE vocabulary_cards
            SET state = ?, stability = ?, difficulty = ?, due_at = ?,
                effective_due_at = ?, last_review_at = ?, repetitions = ?,
                lapses = ?, scheduler_kind = ?, scheduler_version = ?,
                parameters_version = ?, parameter_fingerprint = ?,
                desired_retention = ?
            WHERE id = ?
            """,
            (
                result.after.state.value,
                result.after.stability,
                result.after.difficulty,
                raw_due,
                raw_due,
                reviewed_at,
                result.after.repetitions,
                result.after.lapses,
                result.after.scheduler_kind,
                result.after.scheduler_version,
                result.after.parameters_version,
                result.after.parameter_fingerprint,
                result.after.desired_retention,
                card["id"],
            ),
        )


def _restore_pending_due_state(connection: sqlite3.Connection) -> None:
    pending_rows = connection.execute(
        """
        SELECT entry_id, prompted_at
        FROM review_events
        WHERE status = 'pending'
        ORDER BY prompted_at, id
        """
    ).fetchall()
    for pending in pending_rows:
        prompted_at = _parse_utc(pending["prompted_at"])
        if prompted_at is None:
            continue
        card = connection.execute(
            """
            SELECT id, effective_due_at
            FROM vocabulary_cards
            WHERE entry_id = ? AND direction = 'forward'
            """,
            (pending["entry_id"],),
        ).fetchone()
        effective_due = _parse_required_utc(card["effective_due_at"])
        if prompted_at < effective_due:
            connection.execute(
                "UPDATE vocabulary_cards SET effective_due_at = ? WHERE id = ?",
                (_timestamp(prompted_at), card["id"]),
            )


def _configured_timezone() -> ZoneInfo:
    name = os.environ.get("HERMES_TIMEZONE", "UTC").strip() or "UTC"
    return ZoneInfo(name)


def _reconstruct_active_test(connection: sqlite3.Connection) -> None:
    legacy_session = connection.execute(
        """
        SELECT id, started_at FROM test_sessions
        WHERE status = 'active'
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    if legacy_session is None:
        return
    started_at = _parse_required_utc(legacy_session["started_at"])
    local_date = started_at.astimezone(_configured_timezone()).date().isoformat()
    cursor = connection.execute(
        """
        INSERT INTO study_sessions (
            mode, status, started_at, local_date, legacy_test_session_id
        ) VALUES ('test_forward', 'interrupted', ?, ?, ?)
        """,
        (_timestamp(started_at), local_date, legacy_session["id"]),
    )
    session_id = cursor.lastrowid
    questions = connection.execute(
        """
        SELECT id, entry_id, position
        FROM test_questions
        WHERE session_id = ?
        ORDER BY position, id
        """,
        (legacy_session["id"],),
    ).fetchall()
    for question in questions:
        card = connection.execute(
            """
            SELECT id, state, introduced_local_date
            FROM vocabulary_cards
            WHERE entry_id = ? AND direction = 'forward'
            """,
            (question["entry_id"],),
        ).fetchone()
        attempt = connection.execute(
            """
            SELECT id FROM review_attempts
            WHERE legacy_source = 'test_question' AND legacy_id = ?
            """,
            (question["id"],),
        ).fetchone()
        introduced_local_date = None
        if attempt is None and card["state"] == "new":
            introduced_local_date = local_date
            connection.execute(
                """
                UPDATE vocabulary_cards
                SET introduced_local_date = COALESCE(introduced_local_date, ?)
                WHERE id = ?
                """,
                (local_date, card["id"]),
            )
        connection.execute(
            """
            INSERT INTO study_queue (
                session_id, card_id, position, status,
                completed_attempt_id, legacy_test_question_id,
                introduced_local_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                card["id"],
                question["position"],
                "completed" if attempt else "queued",
                attempt["id"] if attempt else None,
                question["id"],
                introduced_local_date,
            ),
        )


def backfill_v5(connection: sqlite3.Connection) -> None:
    """Create project cards and replay trustworthy v4 evidence transactionally."""

    _create_cards(connection)
    _replay_history(connection)
    _restore_pending_due_state(connection)
    _reconstruct_active_test(connection)


__all__ = ["backfill_v5"]
