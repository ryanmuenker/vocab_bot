from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes_vocab.database import Database
from hermes_vocab.models import (
    CardDirection,
    CardScheduleState,
    Evaluation,
    EvaluationGrade,
    FinalizeStatus,
    ReviewRating,
    StudyMode,
    StudyMutationStatus,
    StudyQueueStatus,
    StudySessionStatus,
    StudyStartStatus,
)
from hermes_vocab.review import ReviewService

NOW = datetime(2026, 7, 20, 14, tzinfo=UTC)
TIMEZONE = ZoneInfo("America/New_York")
EVALUATION = Evaluation(EvaluationGrade.CORRECT, "Accurate.")


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def setup_service(tmp_path: Path) -> tuple[ReviewService, Clock, Database]:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    clock = Clock()
    return ReviewService(database, TIMEZONE, clock), clock, database


def add_card(
    database: Database,
    *,
    entry_id: int,
    card_id: int,
    state: CardScheduleState,
    due: datetime,
    stability: float | None = None,
    difficulty: float | None = None,
    last_review: datetime | None = None,
    repetitions: int = 0,
    lapses: int = 0,
    direction: CardDirection = CardDirection.FORWARD,
    sense_id: int | None = None,
    created_at: datetime | None = None,
    introduced_local_date: str | None = None,
    buried_until_local_date: str | None = None,
) -> int:
    created_at = created_at or NOW - timedelta(days=entry_id)

    def timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    with database.connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO vocabulary_entries (
                id, display_text, normalized_text, date_added, review_status
            ) VALUES (?, ?, ?, ?, 'new')
            """,
            (
                entry_id,
                f"word-{entry_id}",
                f"word-{entry_id}",
                timestamp(created_at),
            ),
        )
        if sense_id is None and direction is CardDirection.REVERSE:
            sense_id = card_id * 10
        if sense_id is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO vocabulary_senses (
                    id, entry_id, definition, part_of_speech,
                    example_sentence, date_added
                ) VALUES (?, ?, ?, 'noun', ?, ?)
                """,
                (
                    sense_id,
                    entry_id,
                    f"Definition {sense_id}.",
                    f"Example {sense_id}.",
                    timestamp(created_at),
                ),
            )
        connection.execute(
            """
            INSERT INTO vocabulary_cards (
                id, entry_id, sense_id, direction, state,
                stability, difficulty, due_at, effective_due_at,
                last_review_at, repetitions, lapses,
                scheduler_kind, scheduler_version, parameters_version,
                parameter_fingerprint, desired_retention,
                introduced_local_date, buried_until_local_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'fsrs-6', 'fsrs-6.3.1-hermes-1',
                      'py-fsrs-6.3.1-default',
                      'sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56',
                      0.9, ?, ?, ?)
            """,
            (
                card_id,
                entry_id,
                sense_id,
                direction.value,
                state.value,
                stability,
                difficulty,
                timestamp(due),
                timestamp(due),
                timestamp(last_review) if last_review else None,
                repetitions,
                lapses,
                introduced_local_date,
                buried_until_local_date,
                timestamp(created_at),
            ),
        )
        connection.commit()
    return card_id


def add_new(database: Database, index: int, **kwargs) -> int:
    kwargs.setdefault("created_at", NOW + timedelta(seconds=index))
    return add_card(
        database,
        entry_id=100 + index,
        card_id=1000 + index,
        state=CardScheduleState.NEW,
        due=NOW - timedelta(days=30),
        **kwargs,
    )


def add_seen(
    database: Database,
    index: int,
    *,
    due: datetime,
    stability: float = 3.0,
    last_review: datetime | None = None,
    **kwargs,
) -> int:
    return add_card(
        database,
        entry_id=200 + index,
        card_id=2000 + index,
        state=CardScheduleState.REVIEW,
        due=due,
        stability=stability,
        difficulty=5.0,
        last_review=last_review or NOW - timedelta(days=3),
        repetitions=1,
        **kwargs,
    )


def deliver_current(service: ReviewService, suffix: str = "1"):
    prompt = service.prepare_current_prompt(
        f"prompt-{suffix}", f"Stable prompt {suffix}."
    )
    assert prompt is not None
    delivered = service.record_delivery(
        prompt.id,
        delivery_id=f"delivery-{suffix}",
        content_fingerprint=f"fingerprint-{suffix}",
    )
    assert delivered is not None
    return delivered


def test_start_snapshots_all_due_plus_five_unseen_and_restart_resumes(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    due_ids = [
        add_seen(database, index, due=NOW - timedelta(days=8 - index))
        for index in range(8)
    ]
    new_ids = [add_new(database, index) for index in range(7)]

    started = service.start()
    restarted = ReviewService(database, TIMEZONE, lambda: NOW).start()

    assert started.status is StudyStartStatus.STARTED
    assert started.snapshot is not None
    assert [item.card.id for item in started.snapshot.queue] == due_ids + new_ids[:5]
    assert restarted.status is StudyStartStatus.RESUMED
    assert restarted.snapshot == started.snapshot
    assert started.snapshot.progress.total == 13
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_cards WHERE introduced_local_date = '2026-07-20'"
        ).fetchone()[0] == 5


def test_ordinary_review_excludes_seen_non_due_and_exit_preserves_due(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    due_id = add_seen(database, 1, due=NOW - timedelta(days=1))
    non_due_id = add_seen(database, 2, due=NOW + timedelta(days=3))
    before = {}
    with database.connect() as connection:
        before = {
            row["id"]: row["effective_due_at"]
            for row in connection.execute(
                "SELECT id, effective_due_at FROM vocabulary_cards"
            )
        }

    result = service.start()
    exited = service.exit()

    assert [item.card.id for item in result.snapshot.queue] == [due_id]
    assert non_due_id not in [item.card.id for item in result.snapshot.queue]
    assert exited is StudyMutationStatus.COMPLETED
    assert service.active_mode() is None
    with database.connect() as connection:
        after = {
            row["id"]: row["effective_due_at"]
            for row in connection.execute(
                "SELECT id, effective_due_at FROM vocabulary_cards"
            )
        }
    assert after == before


def test_exit_cancels_answered_prompt_and_closes_unfinished_queue_without_scheduling(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    card_id = add_seen(database, 1, due=NOW - timedelta(days=1))
    with database.connect() as connection:
        schedule_before = tuple(
            connection.execute(
                """
                SELECT state, stability, difficulty, due_at, effective_due_at,
                       last_review_at, repetitions, lapses
                FROM vocabulary_cards WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
        )
    started = service.start()
    prompt = deliver_current(service)
    assert service.record_answer(prompt.id, "draft answer", EVALUATION) is not None

    assert service.exit() is StudyMutationStatus.COMPLETED

    assert service.answerable_prompt() is None
    assert service.awaiting_rating() is None
    assert service.active_mode() is None
    restarted = service.start()
    assert restarted.status is StudyStartStatus.STARTED
    assert restarted.snapshot is not None
    assert restarted.snapshot.queue[0].card.id == card_id
    assert restarted.snapshot.session_id != started.snapshot.session_id
    with database.connect() as connection:
        assert tuple(
            connection.execute(
                "SELECT status, delivered_at, answered_at FROM study_prompts WHERE id = ?",
                (prompt.id,),
            ).fetchone()
        ) == (
            "cancelled",
            NOW.isoformat().replace("+00:00", "Z"),
            NOW.isoformat().replace("+00:00", "Z"),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM answer_drafts WHERE prompt_id = ?",
            (prompt.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM review_attempts WHERE prompt_id = ?",
            (prompt.id,),
        ).fetchone()[0] == 0
        assert tuple(
            connection.execute(
                """
                SELECT state, stability, difficulty, due_at, effective_due_at,
                       last_review_at, repetitions, lapses
                FROM vocabulary_cards WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
        ) == schedule_before
        assert connection.execute(
            "SELECT COUNT(*) FROM study_queue WHERE session_id = ? AND status IN ('current', 'queued')",
            (started.snapshot.session_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM study_sessions WHERE id = ?",
            (started.snapshot.session_id,),
        ).fetchone()[0] == "exited"


def test_exit_after_partial_progress_carries_remaining_introductions_and_due_card(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    add_seen(database, 1, due=NOW - timedelta(days=2))
    unanswered_due = add_seen(database, 2, due=NOW - timedelta(days=1))
    new_ids = [add_new(database, index) for index in range(6)]
    started = service.start()
    assert started.snapshot is not None
    first_prompt = deliver_current(service, "completed-before-exit")
    assert service.record_answer(first_prompt.id, "answer", EVALUATION) is not None
    assert service.finalize(first_prompt.id, ReviewRating.GOOD).status is FinalizeStatus.COMPLETED
    before_exit = service.snapshot()
    assert before_exit is not None
    remaining_ids = [
        item.card.id
        for item in before_exit.queue
        if item.status in (StudyQueueStatus.CURRENT, StudyQueueStatus.QUEUED)
    ]
    assert remaining_ids == [unanswered_due, *new_ids[:5]]
    with database.connect() as connection:
        schedules_before = {
            row["id"]: tuple(row)[1:]
            for row in connection.execute(
                """
                SELECT id, state, stability, difficulty, due_at, effective_due_at,
                       last_review_at, repetitions, lapses, introduced_local_date
                FROM vocabulary_cards
                WHERE id IN (?, ?, ?, ?, ?, ?)
                ORDER BY id
                """,
                remaining_ids,
            )
        }
        introduced_before = connection.execute(
            """
            SELECT COUNT(*) FROM vocabulary_cards
            WHERE introduced_local_date = '2026-07-20'
            """
        ).fetchone()[0]
        queue_introductions_before = {
            row["card_id"]: row["introduced_local_date"]
            for row in connection.execute(
                """
                SELECT card_id, introduced_local_date
                FROM study_queue
                WHERE session_id = ? AND card_id IN (?, ?, ?, ?, ?, ?)
                ORDER BY position
                """,
                (started.snapshot.session_id, *remaining_ids),
            )
        }

    assert service.exit() is StudyMutationStatus.COMPLETED
    restarted = service.start()

    assert restarted.status is StudyStartStatus.STARTED
    assert restarted.snapshot is not None
    assert restarted.snapshot.session_id != started.snapshot.session_id
    assert [item.card.id for item in restarted.snapshot.queue] == remaining_ids
    assert restarted.snapshot.current_prompt is None
    assert service.answerable_prompt() is None
    assert service.awaiting_rating() is None
    with database.connect() as connection:
        schedules_after = {
            row["id"]: tuple(row)[1:]
            for row in connection.execute(
                """
                SELECT id, state, stability, difficulty, due_at, effective_due_at,
                       last_review_at, repetitions, lapses, introduced_local_date
                FROM vocabulary_cards
                WHERE id IN (?, ?, ?, ?, ?, ?)
                ORDER BY id
                """,
                remaining_ids,
            )
        }
        assert schedules_after == schedules_before
        queue_introductions_after = {
            row["card_id"]: row["introduced_local_date"]
            for row in connection.execute(
                """
                SELECT card_id, introduced_local_date
                FROM study_queue
                WHERE session_id = ? AND card_id IN (?, ?, ?, ?, ?, ?)
                ORDER BY position
                """,
                (restarted.snapshot.session_id, *remaining_ids),
            )
        }
        assert queue_introductions_after == queue_introductions_before
        assert connection.execute(
            """
            SELECT COUNT(*) FROM vocabulary_cards
            WHERE introduced_local_date = '2026-07-20'
            """
        ).fetchone()[0] == introduced_before == 5
        assert connection.execute(
            "SELECT introduced_local_date FROM vocabulary_cards WHERE id = ?",
            (new_ids[5],),
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT status FROM study_sessions WHERE id = ?",
            (started.snapshot.session_id,),
        ).fetchone()[0] == "exited"

    for index in range(len(remaining_ids)):
        prompt = deliver_current(service, f"carryover-{index}")
        assert service.record_answer(prompt.id, "answer", EVALUATION) is not None
        assert service.finalize(prompt.id, ReviewRating.GOOD).status is FinalizeStatus.COMPLETED
    assert service.active_mode() is None
    assert service.start().status is StudyStartStatus.EMPTY


def test_central_bounded_selector_orders_due_then_weak_seen_then_unseen(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    due_id = add_seen(database, 1, due=NOW - timedelta(hours=1), stability=3.0)
    older_strong = add_seen(
        database,
        2,
        due=NOW + timedelta(days=10),
        stability=100.0,
        created_at=NOW - timedelta(days=100),
    )
    newer_weak = add_seen(
        database,
        3,
        due=NOW + timedelta(days=10),
        stability=0.5,
        created_at=NOW - timedelta(days=2),
    )
    unseen = add_new(database, 4)

    selected = service.select_cards(
        maximum_count=4,
        include_seen_non_due=True,
    )

    assert [card.id for card in selected] == [due_id, newer_weak, older_strong, unseen]


def test_selector_supports_direction_distinct_entries_and_shared_daily_quota(
    tmp_path: Path,
) -> None:
    service, clock, database = setup_service(tmp_path)
    for index in range(3):
        add_new(database, index, introduced_local_date="2026-07-20")
    first = add_new(database, 10)
    add_card(
        database,
        entry_id=110,
        card_id=9010,
        state=CardScheduleState.NEW,
        due=NOW - timedelta(days=1),
        direction=CardDirection.REVERSE,
        sense_id=90100,
        created_at=NOW + timedelta(seconds=10),
    )
    second = add_new(database, 11)
    third = add_new(database, 12)

    selected = service.select_cards(maximum_count=5, distinct_entries=True)
    assert [card.id for card in selected] == [first, second]

    clock.value = NOW + timedelta(hours=8)  # UTC midnight, still July 20 in New York.
    assert [card.id for card in service.select_cards(maximum_count=5)] == [
        first,
        9010,
    ]

    clock.value = NOW + timedelta(days=1)
    assert len(service.select_cards(maximum_count=5)) == 4
    reverse = service.select_cards(
        maximum_count=5,
        direction=CardDirection.REVERSE,
        distinct_entries=True,
    )
    assert [card.id for card in reverse] == [9010]
    assert third in [card.id for card in service.select_cards(maximum_count=5)]


def test_finalization_buries_siblings_without_changing_due_and_next_day_reappears(
    tmp_path: Path,
) -> None:
    service, clock, database = setup_service(tmp_path)
    forward = add_seen(database, 1, due=NOW - timedelta(days=1))
    sibling = add_card(
        database,
        entry_id=201,
        card_id=9001,
        state=CardScheduleState.REVIEW,
        stability=2.0,
        difficulty=5.0,
        due=NOW - timedelta(hours=12),
        last_review=NOW - timedelta(days=3),
        repetitions=1,
        direction=CardDirection.REVERSE,
        sense_id=90010,
    )
    service.start()
    prompt = deliver_current(service)
    assert service.record_answer(prompt.id, "answer", EVALUATION) is not None
    with database.connect() as connection:
        sibling_due = connection.execute(
            "SELECT effective_due_at FROM vocabulary_cards WHERE id = ?", (sibling,)
        ).fetchone()[0]

    finalized = service.finalize(prompt.id, ReviewRating.GOOD)

    assert finalized.status is FinalizeStatus.COMPLETED
    assert finalized.snapshot is not None
    assert finalized.snapshot.status is StudySessionStatus.COMPLETED
    assert finalized.snapshot.progress.completed == 1
    assert finalized.snapshot.progress.total == 1
    assert sum(
        item.status is StudyQueueStatus.SKIPPED
        for item in finalized.snapshot.queue
    ) == 1
    with database.connect() as connection:
        sibling_row = connection.execute(
            "SELECT effective_due_at, buried_until_local_date FROM vocabulary_cards WHERE id = ?",
            (sibling,),
        ).fetchone()
    assert tuple(sibling_row) == (sibling_due, "2026-07-20")
    assert sibling not in [card.id for card in service.select_cards()]
    clock.value = NOW + timedelta(days=1)
    assert sibling in [card.id for card in service.select_cards()]
    assert forward != sibling


@pytest.mark.parametrize("prepare_prompt", [False, True])
def test_rollover_without_answerable_prompt_reorders_newly_due_before_unseen_current(
    tmp_path: Path,
    prepare_prompt: bool,
) -> None:
    service, clock, database = setup_service(tmp_path)
    unseen_current = add_new(database, 1)
    started = service.start()
    assert started.snapshot.queue[0].card.id == unseen_current
    prepared = None
    if prepare_prompt:
        prepared = service.prepare_current_prompt(
            "rollover-prepared",
            "Prepared but not delivered.",
        )
        assert prepared is not None
    newly_due = add_seen(database, 20, due=NOW + timedelta(hours=5))
    clock.value = NOW + timedelta(days=1)

    rolled = service.snapshot()

    active = [
        item
        for item in rolled.queue
        if item.status in (StudyQueueStatus.CURRENT, StudyQueueStatus.QUEUED)
    ]
    assert [item.card.id for item in active[:2]] == [newly_due, unseen_current]
    assert active[0].status is StudyQueueStatus.CURRENT
    assert rolled.current_prompt is None
    if prepared is not None:
        with database.connect() as connection:
            assert connection.execute(
                "SELECT status FROM study_prompts WHERE id = ?",
                (prepared.id,),
            ).fetchone()[0] == "cancelled"


def test_retry_attempt_links_to_original_after_restart_and_concurrent_finalize(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    add_seen(database, 1, due=NOW - timedelta(days=1))
    service.start()
    first_prompt = deliver_current(service, "first")
    service.record_answer(first_prompt.id, "first", EVALUATION)

    first = service.finalize(first_prompt.id, ReviewRating.AGAIN)
    restarted = ReviewService(database, TIMEZONE, lambda: NOW)
    retry_prompt = deliver_current(restarted, "retry")
    restarted.record_answer(retry_prompt.id, "second", EVALUATION)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: restarted.finalize(retry_prompt.id, ReviewRating.AGAIN),
                range(2),
            )
        )
    second = next(
        result for result in results if result.status is FinalizeStatus.COMPLETED
    )

    assert first.status is FinalizeStatus.COMPLETED
    assert first.transition is not None and first.transition.retry_same_session is True
    assert sorted(result.status.value for result in results) == ["completed", "stale"]
    assert second.transition is not None and second.transition.retry_same_session is False
    assert second.transition.effective_due >= datetime(2026, 7, 21, 4, tzinfo=UTC)
    with database.connect() as connection:
        attempts = connection.execute(
            """
            SELECT id, after_raw_due_at, after_effective_due_at,
                   is_same_session_retry, retry_of_attempt_id
            FROM review_attempts ORDER BY id
            """
        ).fetchall()
        assert connection.execute(
            "SELECT COUNT(*) FROM study_queue WHERE retry_of_queue_item_id IS NOT NULL"
        ).fetchone()[0] == 1
    assert len(attempts) == 2
    assert attempts[1]["after_raw_due_at"] == second.transition.raw_due.isoformat().replace(
        "+00:00", "Z"
    )
    assert attempts[1]["after_effective_due_at"] == second.transition.effective_due.isoformat().replace(
        "+00:00", "Z"
    )
    assert attempts[1]["is_same_session_retry"] == 1
    assert attempts[1]["retry_of_attempt_id"] == attempts[0]["id"]


def test_duplicate_starts_and_finalizations_are_idempotent_under_concurrency(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    add_seen(database, 1, due=NOW - timedelta(days=1))
    with ThreadPoolExecutor(max_workers=2) as executor:
        starts = list(executor.map(lambda _: service.start(), range(2)))
    assert {result.status for result in starts} == {
        StudyStartStatus.STARTED,
        StudyStartStatus.RESUMED,
    }
    assert starts[0].snapshot.session_id == starts[1].snapshot.session_id
    prompt = deliver_current(service)
    service.record_answer(prompt.id, "answer", EVALUATION)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: service.finalize(prompt.id, ReviewRating.GOOD), range(2))
        )

    assert sorted(result.status.value for result in results) == ["completed", "stale"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 1


def test_rollover_keeps_answerable_prompt_and_reconciles_once_concurrently(
    tmp_path: Path,
) -> None:
    service, clock, database = setup_service(tmp_path)
    current = add_seen(database, 1, due=NOW - timedelta(days=1))
    retained_intro = add_new(database, 1)
    service.start()
    delivered = deliver_current(service)
    newly_due = add_seen(database, 2, due=NOW + timedelta(hours=5))
    add_new(database, 10)
    clock.value = NOW + timedelta(days=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshots = list(executor.map(lambda _: service.snapshot(), range(2)))

    assert all(snapshot.current_prompt.id == delivered.id for snapshot in snapshots)
    queue_ids = [
        item.card.id
        for item in snapshots[0].queue
        if item.status is not StudyQueueStatus.COMPLETED
    ]
    assert queue_ids[0] == current
    assert newly_due in queue_ids
    assert retained_intro in queue_ids
    assert snapshots[0] == snapshots[1]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT local_date FROM study_sessions WHERE status = 'active'"
        ).fetchone()[0] == "2026-07-21"
        assert connection.execute(
            "SELECT COUNT(*) FROM study_queue WHERE session_id = ?",
            (snapshots[0].session_id,),
        ).fetchone()[0] == len(snapshots[0].queue)


@pytest.mark.parametrize(
    ("mode", "operation"),
    [
        (StudyMode.TEST_FORWARD, "snapshot"),
        (StudyMode.TEST_FORWARD, "prepare"),
        (StudyMode.TEST_REVERSE, "snapshot"),
        (StudyMode.TEST_REVERSE, "prepare"),
    ],
)
def test_test_mode_snapshot_and_prompt_do_not_run_review_rollover(
    tmp_path: Path,
    mode: StudyMode,
    operation: str,
) -> None:
    service, _, database = setup_service(tmp_path)
    test_card = add_seen(database, 1, due=NOW + timedelta(days=10))
    review_due = add_seen(database, 2, due=NOW - timedelta(days=1))
    with database.connect() as connection:
        session_id = connection.execute(
            """
            INSERT INTO study_sessions (mode, status, started_at, local_date)
            VALUES (?, 'active', ?, '2026-07-19')
            """,
            (mode.value, NOW.isoformat().replace("+00:00", "Z")),
        ).lastrowid
        queue_id = connection.execute(
            """
            INSERT INTO study_queue (session_id, card_id, position, status)
            VALUES (?, ?, 1, 'current')
            """,
            (session_id, test_card),
        ).lastrowid
        connection.commit()

    if operation == "snapshot":
        snapshot = service.snapshot()
        assert snapshot is not None
        assert snapshot.mode is mode
    else:
        prompt = service.prepare_current_prompt(
            f"{mode.value}-prompt",
            "Stable test prompt.",
        )
        assert prompt is not None
        assert prompt.queue_item_id == queue_id

    with database.connect() as connection:
        assert [
            row["card_id"]
            for row in connection.execute(
                "SELECT card_id FROM study_queue WHERE session_id = ? ORDER BY position",
                (session_id,),
            )
        ] == [test_card]
        assert connection.execute(
            "SELECT local_date FROM study_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()[0] == "2026-07-19"
        assert review_due not in [
            row["card_id"]
            for row in connection.execute(
                "SELECT card_id FROM study_queue WHERE session_id = ?",
                (session_id,),
            )
        ]


def test_queries_distinguish_answerable_awaiting_rating_due_and_progress(
    tmp_path: Path,
) -> None:
    service, _, database = setup_service(tmp_path)
    add_seen(database, 1, due=NOW - timedelta(days=1))
    add_seen(database, 2, due=NOW - timedelta(days=2))
    started = service.start()

    assert service.active_mode() is StudyMode.REVIEW
    assert service.answerable_prompt() is None
    assert service.awaiting_rating() is None
    assert service.due_but_not_answerable() is True
    assert service.progress() == started.snapshot.progress

    prompt = deliver_current(service)
    assert service.answerable_prompt().id == prompt.id
    assert service.due_but_not_answerable() is False
    service.record_answer(prompt.id, "answer", EVALUATION)
    assert service.answerable_prompt() is None
    assert service.awaiting_rating().id == prompt.id


def test_persistence_failure_leaves_answer_retryable_and_queue_unadvanced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, database = setup_service(tmp_path)
    card_id = add_seen(database, 1, due=NOW - timedelta(days=1))
    service.start()
    prompt = deliver_current(service)
    service.record_answer(prompt.id, "answer", EVALUATION)
    original = database.connect

    class FailingConnection:
        def __enter__(self):
            context = original()
            connection = context.__enter__()
            self.context = context
            connection.execute(
                """
                CREATE TEMP TRIGGER fail_attempt BEFORE INSERT ON review_attempts
                BEGIN SELECT RAISE(FAIL, 'injected persistence failure'); END
                """
            )
            return connection

        def __exit__(self, *args):
            return self.context.__exit__(*args)

    monkeypatch.setattr(database, "connect", lambda: FailingConnection())
    failed = service.finalize(prompt.id, ReviewRating.GOOD)
    monkeypatch.setattr(database, "connect", original)

    assert failed.status is FinalizeStatus.STORAGE_ERROR
    assert service.awaiting_rating().id == prompt.id
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_attempts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM study_queue WHERE card_id = ?", (card_id,)
        ).fetchone()[0] == "current"
