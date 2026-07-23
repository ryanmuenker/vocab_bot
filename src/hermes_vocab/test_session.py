from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from .capture import _timestamp
from .database import Database
from .models import (
    Evaluation,
    EvaluationGrade,
    TestCompletionResult,
    TestCompletionStatus,
    TestQuestion,
    TestSession,
    TestSessionSnapshot,
    TestSessionStatus,
    TestSnapshotResult,
    TestSnapshotStatus,
    TestStartResult,
    TestStartStatus,
    TestSummary,
)
from .review import _entry_by_id, _parse_timestamp


_REQUIRED_QUESTIONS = 5


def _session_from_row(row: sqlite3.Row) -> TestSession:
    started_at = _parse_timestamp(row["started_at"])
    assert started_at is not None
    return TestSession(
        id=row["id"],
        status=TestSessionStatus(row["status"]),
        started_at=started_at,
        completed_at=_parse_timestamp(row["completed_at"]),
    )


def _question_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> TestQuestion:
    return TestQuestion(
        id=row["id"],
        session_id=row["session_id"],
        position=row["position"],
        entry=_entry_by_id(connection, row["entry_id"]),
        answer_text=row["answer_text"],
        grade=(
            EvaluationGrade(row["grade"])
            if row["grade"] is not None
            else None
        ),
        feedback=row["evaluation_feedback"],
        answered_at=_parse_timestamp(row["answered_at"]),
    )


def _snapshot(
    connection: sqlite3.Connection,
    session_row: sqlite3.Row,
) -> TestSessionSnapshot:
    question_rows = connection.execute(
        """
        SELECT * FROM test_questions
        WHERE session_id = ?
        ORDER BY position
        """,
        (session_row["id"],),
    ).fetchall()
    questions = tuple(
        _question_from_row(connection, row) for row in question_rows
    )
    current = next(
        (question for question in questions if question.answer_text is None),
        None,
    )
    counts = {grade: 0 for grade in EvaluationGrade}
    for question in questions:
        if question.grade is not None:
            counts[question.grade] += 1
    return TestSessionSnapshot(
        session=_session_from_row(session_row),
        questions=questions,
        current_question=current,
        summary=TestSummary(
            correct=counts[EvaluationGrade.CORRECT],
            partial=counts[EvaluationGrade.PARTIAL],
            incorrect=counts[EvaluationGrade.INCORRECT],
        ),
    )


class TestSessionService:
    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.clock = clock

    def start(self) -> TestStartResult:
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    """
                    SELECT * FROM test_sessions
                    WHERE status = 'active'
                    LIMIT 1
                    """
                ).fetchone()
                if active is not None:
                    snapshot = _snapshot(connection, active)
                    connection.commit()
                    return TestStartResult(
                        TestStartStatus.RESUMED,
                        snapshot=snapshot,
                    )

                pending_review = connection.execute(
                    """
                    SELECT 1 FROM review_events
                    WHERE status = 'pending'
                    LIMIT 1
                    """
                ).fetchone()
                if pending_review is not None:
                    connection.commit()
                    return TestStartResult(
                        TestStartStatus.DAILY_REVIEW_PENDING
                    )

                entry_rows = connection.execute(
                    """
                    SELECT entry.id
                    FROM vocabulary_entries AS entry
                    LEFT JOIN (
                        SELECT
                            question.entry_id,
                            MAX(session.started_at) AS last_tested_at
                        FROM test_questions AS question
                        JOIN test_sessions AS session
                            ON session.id = question.session_id
                        GROUP BY question.entry_id
                    ) AS test_history
                        ON test_history.entry_id = entry.id
                    WHERE EXISTS (
                        SELECT 1
                        FROM vocabulary_senses AS sense
                        WHERE sense.entry_id = entry.id
                    )
                    ORDER BY
                        CASE WHEN test_history.last_tested_at IS NULL THEN 0 ELSE 1 END,
                        test_history.last_tested_at,
                        CASE WHEN entry.last_reviewed IS NULL THEN 0 ELSE 1 END,
                        COALESCE(entry.last_reviewed, entry.date_added),
                        entry.date_added,
                        entry.id
                    LIMIT ?
                    """,
                    (_REQUIRED_QUESTIONS,),
                ).fetchall()
                if len(entry_rows) < _REQUIRED_QUESTIONS:
                    connection.commit()
                    return TestStartResult(
                        TestStartStatus.INSUFFICIENT_LIBRARY,
                        available_count=len(entry_rows),
                    )

                started_at = _timestamp(self.clock())
                session_id = connection.execute(
                    """
                    INSERT INTO test_sessions (status, started_at)
                    VALUES ('active', ?)
                    """,
                    (started_at,),
                ).lastrowid
                assert session_id is not None
                connection.executemany(
                    """
                    INSERT INTO test_questions (
                        session_id, entry_id, position
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        (session_id, row["id"], position)
                        for position, row in enumerate(entry_rows, start=1)
                    ),
                )
                session_row = connection.execute(
                    "SELECT * FROM test_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                assert session_row is not None
                snapshot = _snapshot(connection, session_row)
                connection.commit()
                return TestStartResult(
                    TestStartStatus.STARTED,
                    snapshot=snapshot,
                )
        except sqlite3.Error:
            return TestStartResult(TestStartStatus.STORAGE_ERROR)

    def current(self) -> TestSnapshotResult:
        try:
            with self.database.connect() as connection:
                active = connection.execute(
                    """
                    SELECT * FROM test_sessions
                    WHERE status = 'active'
                    LIMIT 1
                    """
                ).fetchone()
                if active is None:
                    return TestSnapshotResult(TestSnapshotStatus.NONE)
                return TestSnapshotResult(
                    TestSnapshotStatus.ACTIVE,
                    snapshot=_snapshot(connection, active),
                )
        except sqlite3.Error:
            return TestSnapshotResult(TestSnapshotStatus.STORAGE_ERROR)

    def complete(
        self,
        expected_question_id: int,
        answer_text: str,
        evaluation: Evaluation | None,
    ) -> TestCompletionResult:
        if (
            not answer_text.strip()
            or evaluation is None
            or not isinstance(evaluation.grade, EvaluationGrade)
            or not evaluation.feedback.strip()
        ):
            return TestCompletionResult(TestCompletionStatus.INVALID)

        answered_at = _timestamp(self.clock())
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session_row = connection.execute(
                    """
                    SELECT * FROM test_sessions
                    WHERE status = 'active'
                    LIMIT 1
                    """
                ).fetchone()
                if session_row is None:
                    connection.commit()
                    return TestCompletionResult(
                        TestCompletionStatus.NO_ACTIVE
                    )

                current_row = connection.execute(
                    """
                    SELECT * FROM test_questions
                    WHERE session_id = ? AND answer_text IS NULL
                    ORDER BY position
                    LIMIT 1
                    """,
                    (session_row["id"],),
                ).fetchone()
                if (
                    current_row is None
                    or current_row["id"] != expected_question_id
                ):
                    snapshot = _snapshot(connection, session_row)
                    connection.commit()
                    return TestCompletionResult(
                        TestCompletionStatus.STALE,
                        snapshot=snapshot,
                    )

                updated = connection.execute(
                    """
                    UPDATE test_questions
                    SET answer_text = ?,
                        grade = ?,
                        evaluation_feedback = ?,
                        answered_at = ?
                    WHERE id = ?
                      AND session_id = ?
                      AND answer_text IS NULL
                    """,
                    (
                        answer_text,
                        evaluation.grade.value,
                        evaluation.feedback,
                        answered_at,
                        expected_question_id,
                        session_row["id"],
                    ),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return TestCompletionResult(TestCompletionStatus.STALE)

                status = TestCompletionStatus.ADVANCED
                if current_row["position"] == _REQUIRED_QUESTIONS:
                    completed = connection.execute(
                        """
                        UPDATE test_sessions
                        SET status = 'completed', completed_at = ?
                        WHERE id = ? AND status = 'active'
                        """,
                        (answered_at, session_row["id"]),
                    )
                    if completed.rowcount != 1:
                        connection.rollback()
                        return TestCompletionResult(
                            TestCompletionStatus.STALE
                        )
                    status = TestCompletionStatus.COMPLETED

                latest_session = connection.execute(
                    "SELECT * FROM test_sessions WHERE id = ?",
                    (session_row["id"],),
                ).fetchone()
                assert latest_session is not None
                snapshot = _snapshot(connection, latest_session)
                answered_question = next(
                    question
                    for question in snapshot.questions
                    if question.id == expected_question_id
                )
                connection.commit()
                return TestCompletionResult(
                    status=status,
                    snapshot=snapshot,
                    answered_question=answered_question,
                )
        except sqlite3.Error:
            return TestCompletionResult(TestCompletionStatus.STORAGE_ERROR)
