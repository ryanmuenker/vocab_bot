from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from .capture import _entry_from_rows, _timestamp
from .database import Database
from .models import (
    PendingReviewStatus,
    ReviewCompletionResult,
    ReviewCompletionStatus,
    ReviewEvent,
    ReviewPromptResult,
    ReviewPromptStatus,
    VocabularyEntry,
)


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _event_from_row(row: sqlite3.Row) -> ReviewEvent:
    prompted_at = _parse_timestamp(row["prompted_at"])
    assert prompted_at is not None
    return ReviewEvent(
        id=row["id"],
        entry_id=row["entry_id"],
        review_date=date.fromisoformat(row["review_date"]),
        status=row["status"],
        prompted_at=prompted_at,
        answered_at=_parse_timestamp(row["answered_at"]),
        answer_text=row["answer_text"],
    )


def _entry_by_id(
    connection: sqlite3.Connection,
    entry_id: int,
) -> VocabularyEntry:
    entry_row = connection.execute(
        "SELECT * FROM vocabulary_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    assert entry_row is not None
    sense_rows = connection.execute(
        """
        SELECT * FROM vocabulary_senses
        WHERE entry_id = ?
        ORDER BY id
        """,
        (entry_id,),
    ).fetchall()
    return _entry_from_rows(entry_row, sense_rows)


class ReviewService:
    def __init__(
        self,
        database: Database,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.timezone = timezone
        self.clock = clock

    def daily_review(self) -> ReviewPromptResult:
        now = self.clock()
        review_date = now.astimezone(self.timezone).date().isoformat()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current_event = connection.execute(
                    "SELECT * FROM review_events WHERE review_date = ?",
                    (review_date,),
                ).fetchone()
                if current_event is not None:
                    entry = _entry_by_id(connection, current_event["entry_id"])
                    connection.commit()
                    status = (
                        ReviewPromptStatus.PENDING
                        if current_event["status"] == "pending"
                        else ReviewPromptStatus.ALREADY_COMPLETED
                    )
                    return ReviewPromptResult(
                        status,
                        _event_from_row(current_event),
                        entry,
                    )

                connection.execute(
                    """
                    UPDATE review_events
                    SET status = 'missed'
                    WHERE status = 'pending' AND review_date < ?
                    """,
                    (review_date,),
                )
                entry_row = connection.execute(
                    """
                    SELECT * FROM vocabulary_entries
                    ORDER BY
                        CASE WHEN last_reviewed IS NULL THEN 0 ELSE 1 END,
                        COALESCE(last_reviewed, date_added),
                        date_added,
                        id
                    LIMIT 1
                    """
                ).fetchone()
                if entry_row is None:
                    connection.commit()
                    return ReviewPromptResult(ReviewPromptStatus.EMPTY)

                entry = _entry_by_id(connection, entry_row["id"])
                cursor = connection.execute(
                    """
                    INSERT INTO review_events (
                        entry_id, review_date, status, prompted_at
                    ) VALUES (?, ?, 'pending', ?)
                    """,
                    (entry.id, review_date, _timestamp(now)),
                )
                event_row = connection.execute(
                    "SELECT * FROM review_events WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                connection.commit()
                return ReviewPromptResult(
                    ReviewPromptStatus.PENDING,
                    _event_from_row(event_row),
                    entry,
                )
        except sqlite3.Error:
            return ReviewPromptResult(ReviewPromptStatus.STORAGE_ERROR)

    def complete_review(self, answer_text: str) -> ReviewCompletionResult:
        answer = answer_text.strip()
        if not answer:
            return ReviewCompletionResult(ReviewCompletionStatus.INVALID)

        now = self.clock()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                event_row = connection.execute(
                    """
                    SELECT * FROM review_events
                    WHERE status = 'pending'
                    ORDER BY review_date DESC
                    LIMIT 1
                    """
                ).fetchone()
                if event_row is None:
                    connection.commit()
                    return ReviewCompletionResult(
                        ReviewCompletionStatus.NO_PENDING
                    )

                connection.execute(
                    """
                    UPDATE review_events
                    SET status = 'answered', answered_at = ?, answer_text = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (_timestamp(now), answer, event_row["id"]),
                )
                connection.execute(
                    """
                    UPDATE vocabulary_entries
                    SET last_reviewed = ?, review_status = 'reviewed'
                    WHERE id = ?
                    """,
                    (_timestamp(now), event_row["entry_id"]),
                )
                entry = _entry_by_id(connection, event_row["entry_id"])
                connection.commit()
                return ReviewCompletionResult(
                    ReviewCompletionStatus.COMPLETED,
                    entry,
                    answer,
                )
        except sqlite3.Error:
            return ReviewCompletionResult(ReviewCompletionStatus.STORAGE_ERROR)

    def pending_review_status(self) -> PendingReviewStatus:
        try:
            with self.database.connect() as connection:
                pending = connection.execute(
                    "SELECT 1 FROM review_events WHERE status = 'pending' LIMIT 1"
                ).fetchone()
            if pending is None:
                return PendingReviewStatus.NONE
            return PendingReviewStatus.PENDING
        except sqlite3.Error:
            return PendingReviewStatus.STORAGE_ERROR
