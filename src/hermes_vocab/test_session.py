from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .capture import _timestamp
from .database import Database
from .models import (
    CardDirection,
    EvaluationGrade,
    StudyMode,
    StudyPromptStatus,
    StudySnapshot,
    StudyStartResult,
    StudyStartStatus,
    TestSummary,
)
from .review import ReviewService

_REQUIRED_CARDS = 5


def _mode(direction: CardDirection) -> StudyMode:
    if direction is CardDirection.FORWARD:
        return StudyMode.TEST_FORWARD
    if direction is CardDirection.REVERSE:
        return StudyMode.TEST_REVERSE
    raise ValueError(f"unsupported card direction: {direction!r}")


class TestSessionService:
    """Create deterministic directional tests on the shared study queue."""

    def __init__(
        self,
        database: Database,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.timezone = timezone
        self.clock = clock
        self.study = ReviewService(database, timezone, clock=clock)

    def start(self, direction: CardDirection) -> StudyStartResult:
        if not isinstance(direction, CardDirection):
            raise ValueError("direction must be forward or reverse")
        now = self.clock()
        local_date = now.astimezone(self.timezone).date()
        mode = _mode(direction)
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                open_session = self.study._open_session(connection)
                if open_session is not None:
                    if open_session["mode"] != mode.value:
                        connection.commit()
                        return StudyStartResult(StudyStartStatus.CONFLICT)
                    self._prepare_prompt(
                        connection,
                        open_session["id"],
                        now,
                    )
                    snapshot = self.study._snapshot(connection, open_session["id"])
                    connection.commit()
                    return StudyStartResult(StudyStartStatus.RESUMED, snapshot)

                cards = self.study._select_cards(
                    connection,
                    now=now,
                    maximum_count=_REQUIRED_CARDS,
                    direction=direction,
                    distinct_entries=True,
                    only_unseen=True,
                )
                if len(cards) != _REQUIRED_CARDS:
                    connection.commit()
                    return StudyStartResult(
                        StudyStartStatus.EMPTY,
                        available_count=len(cards),
                    )

                session_id = connection.execute(
                    """
                    INSERT INTO study_sessions (mode, status, started_at, local_date)
                    VALUES (?, 'active', ?, ?)
                    """,
                    (mode.value, _timestamp(now), local_date.isoformat()),
                ).lastrowid
                assert session_id is not None
                self.study._enqueue_cards(
                    connection,
                    session_id,
                    cards,
                    local_date,
                )
                connection.execute(
                    """
                    UPDATE study_queue SET status = 'current'
                    WHERE id = (
                        SELECT id FROM study_queue
                        WHERE session_id = ? ORDER BY position LIMIT 1
                    )
                    """,
                    (session_id,),
                )
                self._prepare_prompt(connection, session_id, now)
                snapshot = self.study._snapshot(connection, session_id)
                connection.commit()
                return StudyStartResult(StudyStartStatus.STARTED, snapshot)
        except sqlite3.Error:
            return StudyStartResult(StudyStartStatus.STORAGE_ERROR)

    def prepare_current_prompt(self) -> StudySnapshot | None:
        """Prepare, but never mark delivered, the current test prompt."""
        now = self.clock()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = self.study._open_session(connection)
                if session is None or session["mode"] == StudyMode.REVIEW.value:
                    connection.commit()
                    return None
                self._prepare_prompt(connection, session["id"], now)
                snapshot = self.study._snapshot(connection, session["id"])
                connection.commit()
                return snapshot
        except sqlite3.Error:
            return None

    def summary(self, session_id: int) -> TestSummary | None:
        """Return correctness for the five original questions, never retries."""
        try:
            with self.database.connect() as connection:
                session = connection.execute(
                    "SELECT mode FROM study_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if (
                    session is None
                    or session["mode"] == StudyMode.REVIEW.value
                ):
                    return None
                rows = connection.execute(
                    """
                    SELECT attempt.evaluator_grade
                    FROM study_queue queue
                    JOIN review_attempts attempt
                      ON attempt.id = queue.completed_attempt_id
                    WHERE queue.session_id = ?
                      AND queue.retry_of_queue_item_id IS NULL
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error:
            return None
        counts = {grade: 0 for grade in EvaluationGrade}
        for row in rows:
            if row["evaluator_grade"] is not None:
                counts[EvaluationGrade(row["evaluator_grade"])] += 1
        return TestSummary(
            correct=counts[EvaluationGrade.CORRECT],
            partial=counts[EvaluationGrade.PARTIAL],
            incorrect=counts[EvaluationGrade.INCORRECT],
        )

    def _prepare_prompt(
        self,
        connection: sqlite3.Connection,
        session_id: int,
        now: datetime,
    ) -> None:
        active = connection.execute(
            """
            SELECT 1 FROM study_prompts
            WHERE session_id = ? AND status IN ('prepared', 'delivered', 'answered')
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if active is not None:
            return
        row = connection.execute(
            """
            SELECT q.id AS queue_id, q.position, q.retry_of_queue_item_id,
                   s.mode, e.display_text, sense.definition
            FROM study_queue q
            JOIN study_sessions s ON s.id = q.session_id
            JOIN vocabulary_cards c ON c.id = q.card_id
            JOIN vocabulary_entries e ON e.id = c.entry_id
            LEFT JOIN vocabulary_senses sense ON sense.id = c.sense_id
            WHERE q.session_id = ? AND q.status = 'current'
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return
        if row["mode"] == StudyMode.TEST_FORWARD.value:
            question = f"What does '{row['display_text']}' mean?"
        else:
            if row["definition"] is None:
                raise sqlite3.IntegrityError("reverse test card has no sense definition")
            question = (
                "Which saved word matches this definition?\n"
                f"{row['definition']}"
            )
        position = _REQUIRED_CARDS if row["retry_of_queue_item_id"] else row["position"]
        retry = " · retry" if row["retry_of_queue_item_id"] else ""
        prompt_text = (
            f"Question {position} of {_REQUIRED_CARDS}{retry}\n{question}"
        )
        connection.execute(
            """
            INSERT INTO study_prompts (
                session_id, queue_item_id, prompt_key, prompt_text,
                status, prepared_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                row["queue_id"],
                f"test:{row['mode']}:{session_id}:{row['queue_id']}",
                prompt_text,
                StudyPromptStatus.PREPARED.value,
                _timestamp(now),
            ),
        )


__all__ = ["TestSessionService"]
