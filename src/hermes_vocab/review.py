from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .capture import _entry_from_rows
from .config import DAILY_NEW_CARD_LIMIT
from .database import Database
from .models import (
    CardDirection,
    CardSchedule,
    CardScheduleState,
    Evaluation,
    EvaluationGrade,
    FinalizeResult,
    FinalizeStatus,
    ReviewRating,
    StudyAnswerContext,
    StudyDraftSnapshot,
    StudyCardSnapshot,
    StudyMode,
    StudyMutationStatus,
    StudyProgress,
    StudyPromptSnapshot,
    StudyPromptStatus,
    StudyQueueItemSnapshot,
    StudyQueueStatus,
    StudySessionStatus,
    StudySnapshot,
    StudyStartResult,
    StudyStartStatus,
    VocabularyEntry,
    VocabularySense,
)
from .scheduling import retrievability, transition


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _required_timestamp(value: str) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"missing persisted timestamp: {value!r}")
    return parsed


def _entry_by_id(
    connection: sqlite3.Connection,
    entry_id: int,
) -> VocabularyEntry | None:
    entry = connection.execute(
        "SELECT * FROM vocabulary_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if entry is None:
        return None
    senses = connection.execute(
        "SELECT * FROM vocabulary_senses WHERE entry_id = ? ORDER BY id",
        (entry_id,),
    ).fetchall()
    return _entry_from_rows(entry, senses)


class ReviewService:
    """Transport-independent persisted study queue and FSRS transition service."""

    def __init__(
        self,
        database: Database,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_card_limit: int = DAILY_NEW_CARD_LIMIT,
    ) -> None:
        self.database = database
        self.timezone = timezone
        self.clock = clock
        self.new_card_limit = new_card_limit

    def start(self) -> StudyStartResult:
        now = self.clock()
        local_date = self._local_date(now)
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                open_session = self._open_session(connection)
                if open_session is not None:
                    if open_session["mode"] != StudyMode.REVIEW.value:
                        connection.commit()
                        return StudyStartResult(StudyStartStatus.CONFLICT)
                    self._reconcile_rollover(connection, open_session, now)
                    snapshot = self._snapshot(connection, open_session["id"])
                    connection.commit()
                    return StudyStartResult(StudyStartStatus.RESUMED, snapshot)

                carryover, introduction_dates = self._carryover_cards(
                    connection,
                    local_date,
                )
                cards = carryover + self._select_cards(
                    connection,
                    now=now,
                    distinct_entries=True,
                    excluded_ids={card.id for card in carryover},
                )
                if not cards:
                    connection.commit()
                    return StudyStartResult(StudyStartStatus.EMPTY)
                cursor = connection.execute(
                    """
                    INSERT INTO study_sessions (mode, status, started_at, local_date)
                    VALUES ('review', 'active', ?, ?)
                    """,
                    (_timestamp(now), local_date.isoformat()),
                )
                session_id = cursor.lastrowid
                self._enqueue_cards(
                    connection,
                    session_id,
                    cards,
                    local_date,
                    introduction_dates=introduction_dates,
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
                snapshot = self._snapshot(connection, session_id)
                connection.commit()
                return StudyStartResult(StudyStartStatus.STARTED, snapshot)
        except sqlite3.Error:
            return StudyStartResult(StudyStartStatus.STORAGE_ERROR)

    def select_cards(
        self,
        *,
        maximum_count: int | None = None,
        include_seen_non_due: bool = False,
        direction: CardDirection | None = None,
        distinct_entries: bool = False,
    ) -> tuple[StudyCardSnapshot, ...]:
        now = self.clock()
        try:
            with self.database.connect() as connection:
                return tuple(
                    self._select_cards(
                        connection,
                        now=now,
                        maximum_count=maximum_count,
                        include_seen_non_due=include_seen_non_due,
                        direction=direction,
                        distinct_entries=distinct_entries,
                    )
                )
        except sqlite3.Error:
            return ()

    def snapshot(self) -> StudySnapshot | None:
        now = self.clock()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = self._open_session(connection)
                if session is None:
                    connection.commit()
                    return None
                if session["mode"] == StudyMode.REVIEW.value:
                    self._reconcile_rollover(connection, session, now)
                snapshot = self._snapshot(connection, session["id"])
                connection.commit()
                return snapshot
        except sqlite3.Error:
            return None

    def prepare_current_prompt(
        self,
        prompt_key: str,
        prompt_text: str,
    ) -> StudyPromptSnapshot | None:
        if not prompt_key or not prompt_text.strip():
            return None
        now = self.clock()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = self._open_session(connection)
                if session is None:
                    connection.commit()
                    return None
                if session["mode"] == StudyMode.REVIEW.value:
                    self._reconcile_rollover(connection, session, now)
                queue = connection.execute(
                    """
                    SELECT id FROM study_queue
                    WHERE session_id = ? AND status = 'current'
                    """,
                    (session["id"],),
                ).fetchone()
                if queue is None:
                    connection.commit()
                    return None
                existing = connection.execute(
                    "SELECT * FROM study_prompts WHERE queue_item_id = ?",
                    (queue["id"],),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO study_prompts (
                            session_id, queue_item_id, prompt_key, prompt_text,
                            status, prepared_at
                        ) VALUES (?, ?, ?, ?, 'prepared', ?)
                        """,
                        (
                            session["id"],
                            queue["id"],
                            prompt_key,
                            prompt_text,
                            _timestamp(now),
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM study_prompts WHERE id = ?",
                        (cursor.lastrowid,),
                    ).fetchone()
                result = self._prompt_snapshot(existing)
                connection.commit()
                return result
        except sqlite3.Error:
            return None

    def record_delivery(
        self,
        prompt_id: int,
        *,
        delivery_id: str,
        content_fingerprint: str,
    ) -> StudyPromptSnapshot | None:
        if not delivery_id or not content_fingerprint:
            return None
        now = self.clock()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                prompt = connection.execute(
                    "SELECT * FROM study_prompts WHERE id = ?",
                    (prompt_id,),
                ).fetchone()
                if prompt is None:
                    connection.commit()
                    return None
                if prompt["status"] == StudyPromptStatus.DELIVERED.value:
                    connection.commit()
                    return self._prompt_snapshot(prompt)
                if prompt["status"] != StudyPromptStatus.PREPARED.value:
                    connection.commit()
                    return None
                attempt_number = connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt_number), 0) + 1
                    FROM prompt_delivery_attempts WHERE prompt_id = ?
                    """,
                    (prompt_id,),
                ).fetchone()[0]
                timestamp = _timestamp(now)
                connection.execute(
                    """
                    INSERT INTO prompt_delivery_attempts (
                        prompt_id, attempt_number, status, attempted_at,
                        receipt_at, outbound_delivery_id, content_fingerprint
                    ) VALUES (?, ?, 'delivered', ?, ?, ?, ?)
                    """,
                    (
                        prompt_id,
                        attempt_number,
                        timestamp,
                        timestamp,
                        delivery_id,
                        content_fingerprint,
                    ),
                )
                connection.execute(
                    """
                    UPDATE study_prompts
                    SET status = 'delivered', delivered_at = ?
                    WHERE id = ? AND status = 'prepared'
                    """,
                    (timestamp, prompt_id),
                )
                row = connection.execute(
                    "SELECT * FROM study_prompts WHERE id = ?", (prompt_id,)
                ).fetchone()
                connection.commit()
                return self._prompt_snapshot(row)
        except sqlite3.Error:
            return None

    def record_answer(
        self,
        prompt_id: int,
        submitted_answer: str,
        evaluation: Evaluation,
    ) -> StudyPromptSnapshot | None:
        if (
            not submitted_answer.strip()
            or not isinstance(evaluation.grade, EvaluationGrade)
            or not evaluation.feedback.strip()
        ):
            return None
        now = self.clock()
        timestamp = _timestamp(now)
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                prompt = connection.execute(
                    "SELECT * FROM study_prompts WHERE id = ?", (prompt_id,)
                ).fetchone()
                if prompt is None:
                    connection.commit()
                    return None
                if prompt["status"] == StudyPromptStatus.ANSWERED.value:
                    connection.commit()
                    return self._prompt_snapshot(prompt)
                if prompt["status"] != StudyPromptStatus.DELIVERED.value:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    INSERT INTO answer_drafts (
                        prompt_id, submitted_answer, evaluator_grade,
                        evaluation_feedback, answered_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prompt_id,
                        submitted_answer,
                        evaluation.grade.value,
                        evaluation.feedback,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE study_prompts SET status = 'answered', answered_at = ?
                    WHERE id = ? AND status = 'delivered'
                    """,
                    (timestamp, prompt_id),
                )
                row = connection.execute(
                    "SELECT * FROM study_prompts WHERE id = ?", (prompt_id,)
                ).fetchone()
                connection.commit()
                return self._prompt_snapshot(row)
        except sqlite3.Error:
            return None

    def finalize(self, prompt_id: int, rating: ReviewRating) -> FinalizeResult:
        now = self.clock()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT p.*, q.card_id, q.retry_of_queue_item_id,
                           q.session_id, q.id AS queue_id,
                           original.completed_attempt_id AS retry_of_attempt_id,
                           d.id AS draft_id, d.submitted_answer,
                           d.evaluator_grade, d.evaluation_feedback,
                           c.*
                    FROM study_prompts p
                    JOIN study_queue q ON q.id = p.queue_item_id
                    LEFT JOIN study_queue original
                           ON original.id = q.retry_of_queue_item_id
                    JOIN vocabulary_cards c ON c.id = q.card_id
                    LEFT JOIN answer_drafts d ON d.prompt_id = p.id
                    WHERE p.id = ?
                    """,
                    (prompt_id,),
                ).fetchone()
                if row is None or row["status"] == StudyPromptStatus.COMPLETED.value:
                    connection.commit()
                    return FinalizeResult(FinalizeStatus.STALE)
                if row["status"] != StudyPromptStatus.ANSWERED.value or row["draft_id"] is None:
                    connection.commit()
                    return FinalizeResult(FinalizeStatus.NO_ANSWER)

                before = self._schedule_from_row(row)
                retry_again = (
                    rating is ReviewRating.AGAIN
                    and row["retry_of_queue_item_id"] is not None
                )
                due_floor = self._next_local_midnight(now) if retry_again else None
                result = transition(
                    before,
                    rating,
                    now,
                    same_session_retry=retry_again,
                    due_floor_utc=due_floor,
                )
                attempt_id = self._insert_attempt(
                    connection,
                    row,
                    result,
                    rating,
                    is_same_session_retry=retry_again,
                )
                updated = connection.execute(
                    """
                    UPDATE vocabulary_cards
                    SET state = ?, stability = ?, difficulty = ?, due_at = ?,
                        effective_due_at = ?, last_review_at = ?,
                        repetitions = ?, lapses = ?, scheduler_kind = ?,
                        scheduler_version = ?, parameters_version = ?,
                        parameter_fingerprint = ?, desired_retention = ?
                    WHERE id = ? AND repetitions = ?
                    """,
                    (
                        result.after.state.value,
                        result.after.stability,
                        result.after.difficulty,
                        _timestamp(result.raw_due),
                        _timestamp(result.effective_due),
                        _timestamp(now),
                        result.after.repetitions,
                        result.after.lapses,
                        result.after.scheduler_kind,
                        result.after.scheduler_version,
                        result.after.parameters_version,
                        result.after.parameter_fingerprint,
                        result.after.desired_retention,
                        row["card_id"],
                        before.repetitions,
                    ),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return FinalizeResult(FinalizeStatus.STALE)

                connection.execute(
                    """
                    UPDATE study_prompts SET status = 'completed'
                    WHERE id = ? AND status = 'answered'
                    """,
                    (prompt_id,),
                )
                connection.execute(
                    """
                    UPDATE study_queue
                    SET status = 'completed', completed_attempt_id = ?
                    WHERE id = ? AND status = 'current'
                    """,
                    (attempt_id, row["queue_id"]),
                )
                if result.retry_same_session:
                    next_position = connection.execute(
                        "SELECT COALESCE(MAX(position), 0) + 1 FROM study_queue WHERE session_id = ?",
                        (row["session_id"],),
                    ).fetchone()[0]
                    connection.execute(
                        """
                        INSERT INTO study_queue (
                            session_id, card_id, position, status,
                            retry_of_queue_item_id
                        ) VALUES (?, ?, ?, 'queued', ?)
                        """,
                        (
                            row["session_id"],
                            row["card_id"],
                            next_position,
                            row["queue_id"],
                        ),
                    )
                next_queue = connection.execute(
                    """
                    SELECT id FROM study_queue
                    WHERE session_id = ? AND status = 'queued'
                    ORDER BY position LIMIT 1
                    """,
                    (row["session_id"],),
                ).fetchone()
                if next_queue is None:
                    connection.execute(
                        """
                        UPDATE study_sessions
                        SET status = 'completed', completed_at = ?
                        WHERE id = ? AND status IN ('active', 'interrupted')
                        """,
                        (_timestamp(now), row["session_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE study_queue SET status = 'current' WHERE id = ?",
                        (next_queue["id"],),
                    )
                snapshot = self._snapshot(connection, row["session_id"])
                connection.commit()
                return FinalizeResult(FinalizeStatus.COMPLETED, result, snapshot)
        except sqlite3.Error:
            return FinalizeResult(FinalizeStatus.STORAGE_ERROR)

    def exit(self) -> StudyMutationStatus:
        now = self.clock()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = self._open_session(connection)
                if session is None:
                    connection.commit()
                    return StudyMutationStatus.STALE
                connection.execute(
                    """
                    UPDATE study_prompts SET status = 'cancelled'
                    WHERE session_id = ?
                      AND status IN ('prepared', 'delivered', 'answered')
                    """,
                    (session["id"],),
                )
                connection.execute(
                    """
                    UPDATE study_queue SET status = 'skipped'
                    WHERE session_id = ? AND status IN ('current', 'queued')
                    """,
                    (session["id"],),
                )
                connection.execute(
                    """
                    UPDATE study_sessions SET status = 'exited', completed_at = ?
                    WHERE id = ?
                    """,
                    (_timestamp(now), session["id"]),
                )
                connection.commit()
                return StudyMutationStatus.COMPLETED
        except sqlite3.Error:
            return StudyMutationStatus.STORAGE_ERROR

    def current_answer_context(self) -> StudyAnswerContext | None:
        try:
            with self.database.connect() as connection:
                prompt_row = connection.execute(
                    """
                    SELECT p.*
                    FROM study_prompts p
                    JOIN study_sessions s ON s.id = p.session_id
                    WHERE s.status IN ('active', 'interrupted')
                      AND p.status IN ('delivered', 'answered')
                    ORDER BY p.id DESC LIMIT 1
                    """
                ).fetchone()
                if prompt_row is None:
                    return None
                queue_row = connection.execute(
                    """
                    SELECT q.id AS queue_id, q.position,
                           q.status AS queue_status,
                           q.retry_of_queue_item_id, c.*
                    FROM study_queue q
                    JOIN vocabulary_cards c ON c.id = q.card_id
                    WHERE q.id = ?
                    """,
                    (prompt_row["queue_item_id"],),
                ).fetchone()
                if queue_row is None:
                    return None
                entry = _entry_by_id(connection, queue_row["entry_id"])
                if entry is None:
                    return None
                sense: VocabularySense | None = None
                if queue_row["sense_id"] is not None:
                    sense = next(
                        (
                            item
                            for item in entry.senses
                            if item.id == queue_row["sense_id"]
                        ),
                        None,
                    )
                    if sense is None:
                        return None
                draft_row = connection.execute(
                    "SELECT * FROM answer_drafts WHERE prompt_id = ?",
                    (prompt_row["id"],),
                ).fetchone()
                draft = None
                if draft_row is not None:
                    draft = StudyDraftSnapshot(
                        id=draft_row["id"],
                        submitted_answer=draft_row["submitted_answer"],
                        evaluation=Evaluation(
                            EvaluationGrade(draft_row["evaluator_grade"]),
                            draft_row["evaluation_feedback"],
                        ),
                        answered_at=_required_timestamp(draft_row["answered_at"]),
                    )
                return StudyAnswerContext(
                    prompt=self._prompt_snapshot(prompt_row),
                    queue_item=StudyQueueItemSnapshot(
                        id=queue_row["queue_id"],
                        card=self._card_snapshot(queue_row),
                        position=queue_row["position"],
                        status=StudyQueueStatus(queue_row["queue_status"]),
                        retry_of_queue_item_id=queue_row[
                            "retry_of_queue_item_id"
                        ],
                    ),
                    entry=entry,
                    sense=sense,
                    draft=draft,
                )
        except sqlite3.Error:
            return None

    def answerable_prompt(self) -> StudyPromptSnapshot | None:
        return self._query_prompt(StudyPromptStatus.DELIVERED)

    def awaiting_rating(self) -> StudyPromptSnapshot | None:
        return self._query_prompt(StudyPromptStatus.ANSWERED)

    def active_mode(self) -> StudyMode | None:
        try:
            with self.database.connect() as connection:
                session = self._open_session(connection)
                return StudyMode(session["mode"]) if session else None
        except sqlite3.Error:
            return None

    def due_but_not_answerable(self) -> bool:
        if self.answerable_prompt() is not None:
            return False
        moment = self.clock()
        now = _timestamp(moment)
        local_date = self._local_date(moment).isoformat()
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT 1 FROM vocabulary_cards
                    WHERE state != 'new' AND effective_due_at <= ?
                      AND (buried_until_local_date IS NULL OR buried_until_local_date < ?)
                    LIMIT 1
                    """,
                    (now, local_date),
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False

    def study_was_exited(self) -> bool:
        """True when the learner exited and nothing has restarted study since.

        An explicit exit is a decision. R4 promises a message resubmitted
        after an exit is captured, so the branch that surfaces due work from
        ordinary text must respect it; otherwise the instruction to 'exit,
        then resubmit' cannot be followed and no word can ever be saved while
        anything is due. Any later session — from the ticker or /review —
        replaces this row and re-arms the branch on its own.
        """
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT status FROM study_sessions ORDER BY id DESC LIMIT 1"
                ).fetchone()
                return (
                    row is not None
                    and row["status"] == StudySessionStatus.EXITED.value
                )
        except sqlite3.Error:
            return False

    def due_count(self) -> int:
        """Number of genuinely overdue cards — seen cards past their due instant."""
        moment = self.clock()
        now = _timestamp(moment)
        local_date = self._local_date(moment).isoformat()
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS n FROM vocabulary_cards
                    WHERE state != 'new' AND effective_due_at <= ?
                      AND (buried_until_local_date IS NULL OR buried_until_local_date < ?)
                    """,
                    (now, local_date),
                ).fetchone()
                return row["n"] if row else 0
        except sqlite3.Error:
            return 0

    def progress(self) -> StudyProgress | None:
        snapshot = self.snapshot()
        return snapshot.progress if snapshot else None

    def _query_prompt(self, status: StudyPromptStatus) -> StudyPromptSnapshot | None:
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT p.* FROM study_prompts p
                    JOIN study_sessions s ON s.id = p.session_id
                    WHERE s.status IN ('active', 'interrupted') AND p.status = ?
                    ORDER BY p.id DESC LIMIT 1
                    """,
                    (status.value,),
                ).fetchone()
                return self._prompt_snapshot(row) if row else None
        except sqlite3.Error:
            return None

    def _select_cards(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        maximum_count: int | None = None,
        include_seen_non_due: bool = False,
        direction: CardDirection | None = None,
        distinct_entries: bool = False,
        only_unseen: bool = False,
        excluded_ids: set[int] | None = None,
    ) -> list[StudyCardSnapshot]:
        local_date = self._local_date(now).isoformat()
        parameters: list[object] = []
        direction_sql = ""
        if direction is not None:
            direction_sql = "AND direction = ?"
            parameters.append(direction.value)
        rows = connection.execute(
            f"""
            SELECT * FROM vocabulary_cards
            WHERE 1=1
              {direction_sql}
            """,
            parameters,
        ).fetchall()
        excluded_ids = excluded_ids or set()
        introduced_today = connection.execute(
            "SELECT COUNT(DISTINCT entry_id) FROM vocabulary_cards WHERE introduced_local_date = ?",
            (local_date,),
        ).fetchone()[0]
        remaining_new = (
            None
            if only_unseen
            else max(self.new_card_limit - introduced_today, 0)
        )
        due: list[tuple[tuple[object, ...], StudyCardSnapshot]] = []
        weak: list[tuple[tuple[object, ...], StudyCardSnapshot]] = []
        unseen: list[tuple[tuple[object, ...], StudyCardSnapshot]] = []
        for row in rows:
            if row["id"] in excluded_ids:
                continue
            card = self._card_snapshot(row)
            if card.state is CardScheduleState.NEW:
                if row["introduced_local_date"] is None:
                    unseen.append(((card.created_at, card.id), card))
                continue
            schedule = self._schedule_from_row(row)
            recall = retrievability(schedule, now)
            if card.effective_due <= now:
                due.append(
                    ((card.effective_due, recall, card.created_at, card.id), card)
                )
            elif include_seen_non_due:
                weak.append(((recall, card.created_at, card.id), card))
        due.sort(key=lambda item: item[0])
        weak.sort(key=lambda item: item[0])
        unseen.sort(key=lambda item: item[0])
        if only_unseen:
            ordered = [(card, True) for _, card in unseen]
        else:
            ordered = [(card, False) for _, card in due]
            if include_seen_non_due:
                ordered.extend((card, False) for _, card in weak)
            ordered.extend((card, True) for _, card in unseen)
        selected: list[StudyCardSnapshot] = []
        selected_new = 0
        entries: set[int] = set()
        for card, is_new in ordered:
            if (
                is_new
                and remaining_new is not None
                and selected_new >= remaining_new
            ):
                continue
            if distinct_entries and card.entry_id in entries:
                continue
            entries.add(card.entry_id)
            selected.append(card)
            selected_new += int(is_new)
            if maximum_count is not None and len(selected) >= maximum_count:
                break
        return selected

    def _carryover_cards(
        self,
        connection: sqlite3.Connection,
        local_date: date,
    ) -> tuple[list[StudyCardSnapshot], dict[int, str]]:
        session = connection.execute(
            """
            SELECT id, status FROM study_sessions
            WHERE mode = 'review'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if session is None or session["status"] != StudySessionStatus.EXITED.value:
            return [], {}
        rows = connection.execute(
            """
            SELECT q.position, c.*
            FROM study_queue q
            JOIN vocabulary_cards c ON c.id = q.card_id
            WHERE q.session_id = ?
              AND q.status = 'skipped'
              AND q.retry_of_queue_item_id IS NULL
              AND (
                  c.buried_until_local_date IS NULL
                  OR c.buried_until_local_date < ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM study_queue completed
                  WHERE completed.session_id = q.session_id
                    AND completed.card_id = q.card_id
                    AND completed.status = 'completed'
              )
              AND q.id = (
                  SELECT MAX(latest.id)
                  FROM study_queue latest
                  WHERE latest.session_id = q.session_id
                    AND latest.card_id = q.card_id
                    AND latest.status = 'skipped'
                    AND latest.retry_of_queue_item_id IS NULL
              )
            ORDER BY q.position
            """,
            (session["id"], local_date.isoformat()),
        ).fetchall()
        cards = [self._card_snapshot(row) for row in rows]
        introduction_dates = {
            row["id"]: row["introduced_local_date"]
            for row in rows
            if row["state"] == CardScheduleState.NEW.value
            and row["introduced_local_date"] is not None
        }
        return cards, introduction_dates


    def _enqueue_cards(
        self,
        connection: sqlite3.Connection,
        session_id: int,
        cards: list[StudyCardSnapshot],
        local_date: date,
        *,
        introduction_dates: dict[int, str] | None = None,
    ) -> None:
        for position, card in enumerate(cards, start=1):
            introduced = (
                (introduction_dates or {}).get(card.id, local_date.isoformat())
                if card.state is CardScheduleState.NEW
                else None
            )
            connection.execute(
                """
                INSERT INTO study_queue (
                    session_id, card_id, position, status, introduced_local_date
                ) VALUES (?, ?, ?, 'queued', ?)
                """,
                (session_id, card.id, position, introduced),
            )
            if introduced is not None:
                connection.execute(
                    """
                    UPDATE vocabulary_cards
                    SET introduced_local_date = COALESCE(introduced_local_date, ?)
                    WHERE entry_id = ?
                    """,
                    (introduced, card.entry_id),
                )
    def _reconcile_rollover(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        now: datetime,
    ) -> None:
        local_date = self._local_date(now)
        if session["local_date"] == local_date.isoformat():
            return
        prompt = connection.execute(
            """
            SELECT queue_item_id, status
            FROM study_prompts
            WHERE session_id = ?
              AND status IN ('prepared', 'delivered', 'answered')
            ORDER BY id DESC LIMIT 1
            """,
            (session["id"],),
        ).fetchone()
        active_rows = connection.execute(
            """
            SELECT q.id AS queue_id, q.card_id, q.position,
                   q.status AS queue_status, q.retry_of_queue_item_id,
                   c.*
            FROM study_queue q JOIN vocabulary_cards c ON c.id = q.card_id
            WHERE q.session_id = ? AND q.status IN ('current', 'queued')
            ORDER BY q.position
            """,
            (session["id"],),
        ).fetchall()
        replacement = None
        if prompt is not None and prompt["status"] == StudyPromptStatus.PREPARED.value:
            replacement = next(
                (
                    self._card_snapshot(row)
                    for row in active_rows
                    if row["queue_id"] == prompt["queue_item_id"]
                ),
                None,
            )
            connection.execute(
                "UPDATE study_prompts SET status = 'cancelled' WHERE queue_item_id = ?",
                (prompt["queue_item_id"],),
            )
            connection.execute(
                "UPDATE study_queue SET status = 'skipped', position = position + 200000 WHERE id = ?",
                (prompt["queue_item_id"],),
            )
            active_rows = [
                row for row in active_rows if row["queue_id"] != prompt["queue_item_id"]
            ]
            prompt = None
        pinned_queue_id = (
            prompt["queue_item_id"]
            if prompt is not None
            and prompt["status"]
            in (StudyPromptStatus.DELIVERED.value, StudyPromptStatus.ANSWERED.value)
            else None
        )
        active_ids = {row["card_id"] for row in active_rows}
        selected = self._select_cards(
            connection,
            now=now,
            excluded_ids=active_ids,
        )
        if replacement is not None and replacement.id not in {
            card.id for card in selected
        }:
            selected.append(replacement)

        pinned = [
            ("existing", row)
            for row in active_rows
            if row["queue_id"] == pinned_queue_id
        ]
        due_items = [
            (
                (
                    _required_timestamp(row["effective_due_at"]),
                    _required_timestamp(row["created_at"]),
                    row["card_id"],
                ),
                "existing",
                row,
            )
            for row in active_rows
            if row["queue_id"] != pinned_queue_id and row["state"] != "new"
        ]
        due_items.extend(
            (
                (card.effective_due, card.created_at, card.id),
                "selected",
                card,
            )
            for card in selected
            if card.state is not CardScheduleState.NEW
        )
        new_items = [
            (
                (_required_timestamp(row["created_at"]), row["card_id"]),
                "existing",
                row,
            )
            for row in active_rows
            if row["queue_id"] != pinned_queue_id and row["state"] == "new"
        ]
        new_items.extend(
            (
                (card.created_at, card.id),
                "selected",
                card,
            )
            for card in selected
            if card.state is CardScheduleState.NEW
        )
        due_items.sort(key=lambda item: item[0])
        new_items.sort(key=lambda item: item[0])
        ordered = pinned
        ordered.extend((kind, value) for _, kind, value in due_items)
        ordered.extend((kind, value) for _, kind, value in new_items)

        connection.execute(
            """
            UPDATE study_queue
            SET position = position + 100000,
                status = CASE WHEN status = 'current' THEN 'queued' ELSE status END
            WHERE session_id = ? AND status IN ('current', 'queued')
            """,
            (session["id"],),
        )
        for position, (kind, value) in enumerate(ordered, start=1):
            status = "current" if position == 1 else "queued"
            if kind == "existing":
                connection.execute(
                    "UPDATE study_queue SET position = ?, status = ? WHERE id = ?",
                    (position, status, value["queue_id"]),
                )
                continue
            introduced = (
                local_date.isoformat()
                if value.state is CardScheduleState.NEW
                else None
            )
            connection.execute(
                """
                INSERT INTO study_queue (
                    session_id, card_id, position, status, introduced_local_date
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session["id"], value.id, position, status, introduced),
            )
            if introduced:
                connection.execute(
                    """
                    UPDATE vocabulary_cards
                    SET introduced_local_date = COALESCE(introduced_local_date, ?)
                    WHERE entry_id = ?
                    """,
                    (introduced, value.entry_id),
                )
        connection.execute(
            "UPDATE study_sessions SET local_date = ? WHERE id = ? AND local_date != ?",
            (local_date.isoformat(), session["id"], local_date.isoformat()),
        )

    def _snapshot(self, connection: sqlite3.Connection, session_id: int) -> StudySnapshot:
        session = connection.execute(
            "SELECT * FROM study_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        queue_rows = connection.execute(
            """
            SELECT q.id AS queue_id, q.position, q.status AS queue_status,
                   q.retry_of_queue_item_id, c.*
            FROM study_queue q JOIN vocabulary_cards c ON c.id = q.card_id
            WHERE q.session_id = ? ORDER BY q.position
            """,
            (session_id,),
        ).fetchall()
        queue = tuple(
            StudyQueueItemSnapshot(
                id=row["queue_id"],
                card=self._card_snapshot(row),
                position=row["position"],
                status=StudyQueueStatus(row["queue_status"]),
                retry_of_queue_item_id=row["retry_of_queue_item_id"],
            )
            for row in queue_rows
        )
        prompt_row = connection.execute(
            """
            SELECT * FROM study_prompts
            WHERE session_id = ? AND status IN ('prepared', 'delivered', 'answered')
            ORDER BY id DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        progress_items = (
            queue
            if session["mode"] == StudyMode.REVIEW.value
            else tuple(
                item for item in queue if item.retry_of_queue_item_id is None
            )
        )
        completed = sum(
            item.status is StudyQueueStatus.COMPLETED for item in progress_items
        )
        total = sum(
            item.status is not StudyQueueStatus.SKIPPED for item in progress_items
        )
        return StudySnapshot(
            session_id=session["id"],
            mode=StudyMode(session["mode"]),
            status=StudySessionStatus(session["status"]),
            local_date=date.fromisoformat(session["local_date"]),
            queue=queue,
            current_prompt=self._prompt_snapshot(prompt_row) if prompt_row else None,
            progress=StudyProgress(completed=completed, total=total),
        )

    @staticmethod
    def _open_session(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM study_sessions
            WHERE status IN ('active', 'interrupted')
            ORDER BY id LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _card_snapshot(row: sqlite3.Row) -> StudyCardSnapshot:
        return StudyCardSnapshot(
            id=row["id"],
            entry_id=row["entry_id"],
            sense_id=row["sense_id"],
            direction=CardDirection(row["direction"]),
            state=CardScheduleState(row["state"]),
            stability=row["stability"],
            difficulty=row["difficulty"],
            due=_required_timestamp(row["due_at"]),
            effective_due=_required_timestamp(row["effective_due_at"]),
            last_review=_parse_timestamp(row["last_review_at"]),
            repetitions=row["repetitions"],
            lapses=row["lapses"],
            created_at=_required_timestamp(row["created_at"]),
        )

    @staticmethod
    def _prompt_snapshot(row: sqlite3.Row) -> StudyPromptSnapshot:
        return StudyPromptSnapshot(
            id=row["id"],
            session_id=row["session_id"],
            queue_item_id=row["queue_item_id"],
            prompt_key=row["prompt_key"],
            prompt_text=row["prompt_text"],
            status=StudyPromptStatus(row["status"]),
            prepared_at=_required_timestamp(row["prepared_at"]),
            delivered_at=_parse_timestamp(row["delivered_at"]),
            answered_at=_parse_timestamp(row["answered_at"]),
        )

    @staticmethod
    def _schedule_from_row(row: sqlite3.Row) -> CardSchedule:
        return CardSchedule(
            state=CardScheduleState(row["state"]),
            stability=row["stability"],
            difficulty=row["difficulty"],
            due=_required_timestamp(row["due_at"]),
            last_review=_parse_timestamp(row["last_review_at"]),
            repetitions=row["repetitions"],
            lapses=row["lapses"],
            scheduler_kind=row["scheduler_kind"],
            scheduler_version=row["scheduler_version"],
            parameters_version=row["parameters_version"],
            parameter_fingerprint=row["parameter_fingerprint"],
            desired_retention=row["desired_retention"],
        )

    def _insert_attempt(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        result,
        rating: ReviewRating,
        *,
        is_same_session_retry: bool,
    ) -> int:
        before = result.before
        cursor = connection.execute(
            """
            INSERT INTO review_attempts (
                card_id, session_id, queue_item_id, prompt_id, answer_draft_id,
                source, rating, submitted_answer, evaluator_grade,
                evaluation_feedback, reviewed_at,
                before_state, before_stability, before_difficulty,
                before_due_at, before_effective_due_at, before_last_review_at,
                before_repetitions, before_lapses,
                after_state, after_stability, after_difficulty,
                after_raw_due_at, after_effective_due_at, after_last_review_at,
                after_repetitions, after_lapses,
                scheduler_kind, scheduler_version, parameters_version,
                parameter_fingerprint, desired_retention,
                is_same_session_retry, retry_of_attempt_id, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, 'review', ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["card_id"],
                row["session_id"],
                row["queue_id"],
                row["id"],
                row["draft_id"],
                rating.value,
                row["submitted_answer"],
                row["evaluator_grade"],
                row["evaluation_feedback"],
                _timestamp(result.reviewed_at),
                before.state.value,
                before.stability,
                before.difficulty,
                _timestamp(before.due),
                row["effective_due_at"],
                _timestamp(before.last_review) if before.last_review else None,
                before.repetitions,
                before.lapses,
                result.after.state.value,
                result.after.stability,
                result.after.difficulty,
                _timestamp(result.raw_due),
                _timestamp(result.effective_due),
                _timestamp(result.reviewed_at),
                result.after.repetitions,
                result.after.lapses,
                result.after.scheduler_kind,
                result.after.scheduler_version,
                result.after.parameters_version,
                result.after.parameter_fingerprint,
                result.after.desired_retention,
                int(is_same_session_retry),
                row["retry_of_attempt_id"],
                _timestamp(result.reviewed_at),
            ),
        )
        return cursor.lastrowid

    def _local_date(self, now: datetime) -> date:
        return now.astimezone(self.timezone).date()

    def _next_local_midnight(self, now: datetime) -> datetime:
        next_date = self._local_date(now) + timedelta(days=1)
        return datetime.combine(next_date, time.min, self.timezone).astimezone(UTC)


__all__ = ["ReviewService"]
