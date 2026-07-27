"""Snapshot format v2: the full v5 spaced-review state as a canonical JSON
document that this package and ``worker/src/domain/snapshot.ts`` both produce
and consume byte-identically.

The wire domain is deliberately narrow: null, safe non-negative integers, and
strings. SQLite REAL columns (the FSRS scalars) travel as canonical decimal
strings because JavaScript renders the double 1.0 as ``"1"`` while Python
renders it as ``"1.0"``; encoding them as text removes every number-formatting
divergence from the digest.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

MAX_SAFE_INTEGER = 9_007_199_254_740_991
SCHEMA_VERSION = 5
FORMAT_VERSION = 2

_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REAL = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]+$")

# Band in which Python and JavaScript both render doubles as plain decimals.
_REAL_MIN = 1e-4
_REAL_MAX = 1e16

_GRADES = ("correct", "partial", "incorrect")
_CARD_STATES = ("new", "review", "relearning")

# Columns are (json key, sql column, kind[, enum values]). Kinds mirror
# worker/src/domain/snapshot.ts exactly.
_ENTRY_COLUMNS = (
    ("id", "id", "id"),
    ("displayText", "display_text", "text"),
    ("normalizedText", "normalized_text", "text"),
    ("dateAdded", "date_added", "ts"),
    ("lastReviewed", "last_reviewed", "tsNull"),
    ("reviewStatus", "review_status", "enum", ("new", "reviewed")),
)

_SENSE_COLUMNS = (
    ("id", "id", "id"),
    ("entryId", "entry_id", "id"),
    ("definition", "definition", "text"),
    ("partOfSpeech", "part_of_speech", "text"),
    ("exampleSentence", "example_sentence", "text"),
    ("sourceContext", "source_context", "textNull"),
    ("dateAdded", "date_added", "ts"),
)

_CARD_COLUMNS = (
    ("id", "id", "id"),
    ("entryId", "entry_id", "id"),
    ("senseId", "sense_id", "idNull"),
    ("direction", "direction", "enum", ("forward", "reverse")),
    ("state", "state", "enum", _CARD_STATES),
    ("stability", "stability", "realNull"),
    ("difficulty", "difficulty", "realNull"),
    ("dueAt", "due_at", "ts"),
    ("effectiveDueAt", "effective_due_at", "ts"),
    ("lastReviewAt", "last_review_at", "tsNull"),
    ("repetitions", "repetitions", "int"),
    ("lapses", "lapses", "int"),
    ("schedulerKind", "scheduler_kind", "text"),
    ("schedulerVersion", "scheduler_version", "text"),
    ("parametersVersion", "parameters_version", "text"),
    ("parameterFingerprint", "parameter_fingerprint", "text"),
    ("desiredRetention", "desired_retention", "real"),
    ("introducedLocalDate", "introduced_local_date", "dateNull"),
    ("buriedUntilLocalDate", "buried_until_local_date", "dateNull"),
    ("createdAt", "created_at", "ts"),
)

_STUDY_SESSION_COLUMNS = (
    ("id", "id", "id"),
    ("mode", "mode", "enum", ("review", "test_forward", "test_reverse")),
    ("status", "status", "enum", ("active", "interrupted", "completed", "exited")),
    ("startedAt", "started_at", "ts"),
    ("completedAt", "completed_at", "tsNull"),
    ("localDate", "local_date", "date"),
    ("legacyTestSessionId", "legacy_test_session_id", "idNull"),
)

_STUDY_QUEUE_COLUMNS = (
    ("id", "id", "id"),
    ("sessionId", "session_id", "id"),
    ("cardId", "card_id", "id"),
    ("position", "position", "int"),
    ("status", "status", "enum", ("queued", "current", "completed", "skipped")),
    ("retryOfQueueItemId", "retry_of_queue_item_id", "idNull"),
    ("completedAttemptId", "completed_attempt_id", "idNull"),
    ("legacyTestQuestionId", "legacy_test_question_id", "idNull"),
    ("introducedLocalDate", "introduced_local_date", "dateNull"),
)

_STUDY_PROMPT_COLUMNS = (
    ("id", "id", "id"),
    ("sessionId", "session_id", "id"),
    ("queueItemId", "queue_item_id", "id"),
    ("promptKey", "prompt_key", "text"),
    ("promptText", "prompt_text", "prose"),
    (
        "status",
        "status",
        "enum",
        ("prepared", "delivered", "answered", "completed", "failed", "cancelled"),
    ),
    ("preparedAt", "prepared_at", "ts"),
    ("deliveredAt", "delivered_at", "tsNull"),
    ("answeredAt", "answered_at", "tsNull"),
)

_DELIVERY_ATTEMPT_COLUMNS = (
    ("id", "id", "id"),
    ("promptId", "prompt_id", "id"),
    ("attemptNumber", "attempt_number", "int"),
    ("status", "status", "enum", ("unknown", "failed", "delivered")),
    ("attemptedAt", "attempted_at", "ts"),
    ("receiptAt", "receipt_at", "tsNull"),
    ("outboundDeliveryId", "outbound_delivery_id", "textNull"),
    ("contentFingerprint", "content_fingerprint", "textNull"),
    ("errorText", "error_text", "textNull"),
)

_ANSWER_DRAFT_COLUMNS = (
    ("id", "id", "id"),
    ("promptId", "prompt_id", "id"),
    ("submittedAnswer", "submitted_answer", "prose"),
    ("evaluatorGrade", "evaluator_grade", "enum", _GRADES),
    ("evaluationFeedback", "evaluation_feedback", "prose"),
    ("answeredAt", "answered_at", "ts"),
    ("createdAt", "created_at", "ts"),
)

_REVIEW_ATTEMPT_COLUMNS = (
    ("id", "id", "id"),
    ("cardId", "card_id", "id"),
    ("sessionId", "session_id", "idNull"),
    ("queueItemId", "queue_item_id", "idNull"),
    ("promptId", "prompt_id", "idNull"),
    ("answerDraftId", "answer_draft_id", "idNull"),
    ("source", "source", "enum", ("review", "test_forward", "test_reverse", "migration")),
    ("rating", "rating", "enum", ("again", "hard", "good", "easy")),
    ("submittedAnswer", "submitted_answer", "proseNull"),
    ("evaluatorGrade", "evaluator_grade", "enumNull", _GRADES),
    ("evaluationFeedback", "evaluation_feedback", "proseNull"),
    ("reviewedAt", "reviewed_at", "ts"),
    ("beforeState", "before_state", "enum", _CARD_STATES),
    ("beforeStability", "before_stability", "realNull"),
    ("beforeDifficulty", "before_difficulty", "realNull"),
    ("beforeDueAt", "before_due_at", "ts"),
    ("beforeEffectiveDueAt", "before_effective_due_at", "ts"),
    ("beforeLastReviewAt", "before_last_review_at", "tsNull"),
    ("beforeRepetitions", "before_repetitions", "int"),
    ("beforeLapses", "before_lapses", "int"),
    ("afterState", "after_state", "enum", ("review", "relearning")),
    ("afterStability", "after_stability", "real"),
    ("afterDifficulty", "after_difficulty", "real"),
    ("afterRawDueAt", "after_raw_due_at", "ts"),
    ("afterEffectiveDueAt", "after_effective_due_at", "ts"),
    ("afterLastReviewAt", "after_last_review_at", "ts"),
    ("afterRepetitions", "after_repetitions", "int"),
    ("afterLapses", "after_lapses", "int"),
    ("schedulerKind", "scheduler_kind", "text"),
    ("schedulerVersion", "scheduler_version", "text"),
    ("parametersVersion", "parameters_version", "text"),
    ("parameterFingerprint", "parameter_fingerprint", "text"),
    ("desiredRetention", "desired_retention", "real"),
    ("isSameSessionRetry", "is_same_session_retry", "int"),
    ("retryOfAttemptId", "retry_of_attempt_id", "idNull"),
    ("legacySource", "legacy_source", "textNull"),
    ("legacyId", "legacy_id", "idNull"),
    ("createdAt", "created_at", "ts"),
)

_REVIEW_EVENT_COLUMNS = (
    ("id", "id", "id"),
    ("entryId", "entry_id", "id"),
    ("reviewDate", "review_date", "date"),
    ("status", "status", "enum", ("pending", "answered", "missed")),
    ("promptedAt", "prompted_at", "ts"),
    ("answeredAt", "answered_at", "tsNull"),
    ("answerText", "answer_text", "textNull"),
    ("grade", "grade", "enumNull", _GRADES),
    ("evaluationFeedback", "evaluation_feedback", "proseNull"),
)

_TEST_SESSION_COLUMNS = (
    ("id", "id", "id"),
    ("status", "status", "enum", ("active", "completed")),
    ("startedAt", "started_at", "ts"),
    ("completedAt", "completed_at", "tsNull"),
)

_TEST_QUESTION_COLUMNS = (
    ("id", "id", "id"),
    ("sessionId", "session_id", "id"),
    ("entryId", "entry_id", "id"),
    ("position", "position", "int"),
    ("answerText", "answer_text", "proseNull"),
    ("grade", "grade", "enumNull", _GRADES),
    ("evaluationFeedback", "evaluation_feedback", "proseNull"),
    ("answeredAt", "answered_at", "tsNull"),
)

# Dependency order: importing top to bottom satisfies every foreign key except
# the study_queue <-> review_attempts cycle, which needs deferral.
TABLES: tuple[tuple[str, str, tuple[tuple[Any, ...], ...]], ...] = (
    ("entries", "vocabulary_entries", _ENTRY_COLUMNS),
    ("senses", "vocabulary_senses", _SENSE_COLUMNS),
    ("reviewEvents", "review_events", _REVIEW_EVENT_COLUMNS),
    ("testSessions", "test_sessions", _TEST_SESSION_COLUMNS),
    ("testQuestions", "test_questions", _TEST_QUESTION_COLUMNS),
    ("cards", "vocabulary_cards", _CARD_COLUMNS),
    ("studySessions", "study_sessions", _STUDY_SESSION_COLUMNS),
    ("studyQueue", "study_queue", _STUDY_QUEUE_COLUMNS),
    ("studyPrompts", "study_prompts", _STUDY_PROMPT_COLUMNS),
    ("deliveryAttempts", "prompt_delivery_attempts", _DELIVERY_ATTEMPT_COLUMNS),
    ("answerDrafts", "answer_drafts", _ANSWER_DRAFT_COLUMNS),
    ("reviewAttempts", "review_attempts", _REVIEW_ATTEMPT_COLUMNS),
)

_NULLABLE_KINDS = frozenset(
    {"idNull", "textNull", "proseNull", "tsNull", "dateNull", "realNull", "enumNull"}
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_envelope(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict) or set(value) != {"sha256", "snapshot"}:
        raise ValueError("invalid export envelope")
    digest = value["sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("invalid snapshot digest")
    snapshot = validate_snapshot(value["snapshot"])
    if snapshot_sha256(snapshot) != digest:
        raise ValueError("snapshot digest mismatch")
    return {"sha256": digest, "snapshot": snapshot}


def _validate_unicode(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("lone Unicode surrogate")


def _validate_jcs_value(value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not 0 <= value <= MAX_SAFE_INTEGER:
            raise ValueError("integer outside JavaScript safe range")
        return
    if isinstance(value, str):
        _validate_unicode(value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_jcs_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("non-string JSON key")
            _validate_unicode(key)
            _validate_jcs_value(item)
        return
    raise ValueError("unsupported snapshot value")


def canonical_bytes(snapshot: dict[str, Any]) -> bytes:
    _validate_jcs_value(snapshot)
    return json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8", "strict")


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(snapshot)).hexdigest()


def encode_real(value: float) -> str:
    """Render a double as the canonical decimal string shared with JavaScript.

    Both runtimes emit shortest round-tripping digits, so restricting the
    magnitude to the band where neither switches to exponent notation makes the
    two renderings identical.
    """
    if isinstance(value, bool) or not isinstance(value, float):
        raise ValueError("snapshot real must be a float")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("real outside the snapshot domain")
    if value < 0 or (value != 0 and not _REAL_MIN <= value < _REAL_MAX):
        raise ValueError("real outside the snapshot domain")
    rendered = repr(value)
    if not _REAL.match(rendered):
        raise ValueError("real is not a canonical decimal")
    return rendered


def decode_real(value: str) -> float:
    if not isinstance(value, str) or not _REAL.match(value):
        raise ValueError("real is not a canonical decimal")
    parsed = float(value)
    if encode_real(parsed) != value:
        raise ValueError("real is not a canonical decimal")
    return parsed


def _identifier(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SAFE_INTEGER:
        raise ValueError("invalid snapshot identifier")
    return value


def _validate_column(value: Any, column: tuple[Any, ...]) -> None:
    kind = column[2]
    if value is None:
        if kind not in _NULLABLE_KINDS:
            raise ValueError(f"null in non-nullable column {column[0]}")
        return
    if kind in {"id", "idNull", "int"}:
        _identifier(value)
        return
    if kind in {"enum", "enumNull"}:
        if value not in column[3]:
            raise ValueError(f"invalid value for column {column[0]}")
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid text in column {column[0]}")
    _validate_unicode(value)
    if kind in {"text", "textNull"}:
        return
    if kind in {"prose", "proseNull"}:
        # Mirrors SQLite's `length(trim(x)) > 0`, which strips spaces only.
        if not value.strip(" "):
            raise ValueError(f"blank text in column {column[0]}")
        return
    if kind in {"ts", "tsNull"}:
        if not _UTC_TIMESTAMP.match(value):
            raise ValueError(f"invalid timestamp in column {column[0]}")
        return
    if kind in {"date", "dateNull"}:
        if not _DATE.match(value):
            raise ValueError(f"invalid local date in column {column[0]}")
        return
    decode_real(value)


def _rows(value: Any, columns: tuple[tuple[Any, ...], ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("snapshot table must be an array")
    names = {column[0] for column in columns}
    rows: list[dict[str, Any]] = []
    previous = -1
    for row in value:
        if not isinstance(row, dict) or set(row) != names:
            raise ValueError("invalid snapshot row shape")
        for column in columns:
            _validate_column(row[column[0]], column)
        if row["id"] <= previous:
            raise ValueError("snapshot rows must have unique ascending IDs")
        previous = row["id"]
        rows.append(row)
    return rows


def _require_unique(values: list[Any], message: str) -> None:
    present = [value for value in values if value is not None]
    if len(set(present)) != len(present):
        raise ValueError(message)


def _validate_semantics(snapshot: dict[str, Any]) -> None:
    entry_ids = {row["id"] for row in snapshot["entries"]}
    sense_entry = {row["id"]: row["entryId"] for row in snapshot["senses"]}
    card_ids = {row["id"] for row in snapshot["cards"]}
    study_session_ids = {row["id"] for row in snapshot["studySessions"]}
    queue_ids = {row["id"] for row in snapshot["studyQueue"]}
    prompt_ids = {row["id"] for row in snapshot["studyPrompts"]}
    draft_ids = {row["id"] for row in snapshot["answerDrafts"]}
    attempt_ids = {row["id"] for row in snapshot["reviewAttempts"]}
    test_session_ids = {row["id"] for row in snapshot["testSessions"]}
    test_question_ids = {row["id"] for row in snapshot["testQuestions"]}

    _require_unique(
        [row["normalizedText"] for row in snapshot["entries"]], "duplicate normalized entry"
    )
    for row in snapshot["senses"]:
        if row["entryId"] not in entry_ids:
            raise ValueError("orphan vocabulary sense")

    for row in snapshot["reviewEvents"]:
        if row["entryId"] not in entry_ids:
            raise ValueError("orphan review event")
        answered = row["status"] == "answered"
        fields = (row["answeredAt"], row["answerText"], row["grade"], row["evaluationFeedback"])
        if any((field is not None) != answered for field in fields):
            raise ValueError("review event answer fields disagree with status")
    _require_unique([row["reviewDate"] for row in snapshot["reviewEvents"]], "duplicate review date")

    active_tests = 0
    for row in snapshot["testSessions"]:
        if (row["status"] == "active") == (row["completedAt"] is not None):
            raise ValueError("invalid legacy test session")
        active_tests += row["status"] == "active"
    if active_tests > 1:
        raise ValueError("multiple active legacy tests")

    positions: list[str] = []
    question_entries: list[str] = []
    for row in snapshot["testQuestions"]:
        if row["entryId"] not in entry_ids or row["sessionId"] not in test_session_ids:
            raise ValueError("invalid legacy test question relationship")
        if not 1 <= row["position"] <= 5:
            raise ValueError("invalid legacy test question position")
        positions.append(f"{row['sessionId']}:{row['position']}")
        question_entries.append(f"{row['sessionId']}:{row['entryId']}")
        fields = (row["answerText"], row["grade"], row["evaluationFeedback"], row["answeredAt"])
        if any(field is not None for field in fields) and any(field is None for field in fields):
            raise ValueError("partial legacy test answer")
    _require_unique(positions, "duplicate legacy test question identity")
    _require_unique(question_entries, "duplicate legacy test question identity")

    forward_entries: list[int] = []
    reverse_senses: list[int] = []
    for row in snapshot["cards"]:
        if row["entryId"] not in entry_ids:
            raise ValueError("orphan vocabulary card")
        if row["direction"] == "forward":
            if row["senseId"] is not None:
                raise ValueError("forward card carries a sense")
            forward_entries.append(row["entryId"])
        else:
            if sense_entry.get(row["senseId"]) != row["entryId"]:
                raise ValueError("reverse card sense does not belong to its entry")
            reverse_senses.append(row["senseId"])
        stability = None if row["stability"] is None else decode_real(row["stability"])
        difficulty = None if row["difficulty"] is None else decode_real(row["difficulty"])
        fresh = (
            row["state"] == "new"
            and stability is None
            and difficulty is None
            and row["lastReviewAt"] is None
            and row["repetitions"] == 0
            and row["lapses"] == 0
        )
        seen = (
            row["state"] != "new"
            and stability is not None
            and stability > 0
            and difficulty is not None
            and 1 <= difficulty <= 10
            and row["lastReviewAt"] is not None
            and row["repetitions"] >= 1
        )
        if not fresh and not seen:
            raise ValueError("card state disagrees with its FSRS scalars")
        if row["lapses"] > row["repetitions"]:
            raise ValueError("card has more lapses than repetitions")
        if not 0 < decode_real(row["desiredRetention"]) < 1:
            raise ValueError("card desired retention out of range")
    _require_unique(forward_entries, "multiple forward cards for one entry")
    _require_unique(reverse_senses, "multiple reverse cards for one sense")

    open_sessions = 0
    for row in snapshot["studySessions"]:
        is_open = row["status"] in {"active", "interrupted"}
        if is_open == (row["completedAt"] is not None):
            raise ValueError("study session completion disagrees with status")
        open_sessions += is_open
        if row["legacyTestSessionId"] is not None and row["legacyTestSessionId"] not in test_session_ids:
            raise ValueError("study session references a missing legacy test")
    if open_sessions > 1:
        raise ValueError("multiple open study sessions")
    _require_unique(
        [row["legacyTestSessionId"] for row in snapshot["studySessions"]],
        "duplicate legacy test session link",
    )

    queue_positions: list[str] = []
    current_sessions: list[int] = []
    for row in snapshot["studyQueue"]:
        if row["sessionId"] not in study_session_ids or row["cardId"] not in card_ids:
            raise ValueError("orphan study queue item")
        if row["position"] < 1:
            raise ValueError("invalid study queue position")
        queue_positions.append(f"{row['sessionId']}:{row['position']}")
        if row["status"] == "current":
            current_sessions.append(row["sessionId"])
        if row["retryOfQueueItemId"] is not None and (
            row["retryOfQueueItemId"] == row["id"] or row["retryOfQueueItemId"] not in queue_ids
        ):
            raise ValueError("invalid study queue retry link")
        if (row["status"] == "completed") != (row["completedAttemptId"] is not None):
            raise ValueError("study queue completion disagrees with status")
        if row["completedAttemptId"] is not None and row["completedAttemptId"] not in attempt_ids:
            raise ValueError("study queue references a missing attempt")
        if row["legacyTestQuestionId"] is not None and row["legacyTestQuestionId"] not in test_question_ids:
            raise ValueError("study queue references a missing legacy question")
    _require_unique(queue_positions, "duplicate study queue position")
    _require_unique(current_sessions, "multiple current study queue items")
    for field, message in (
        ("retryOfQueueItemId", "duplicate study queue retry"),
        ("completedAttemptId", "duplicate study queue attempt link"),
        ("legacyTestQuestionId", "duplicate legacy test question link"),
    ):
        _require_unique([row[field] for row in snapshot["studyQueue"]], message)

    active_prompts = 0
    for row in snapshot["studyPrompts"]:
        if row["sessionId"] not in study_session_ids or row["queueItemId"] not in queue_ids:
            raise ValueError("orphan study prompt")
        if row["status"] == "prepared" and row["deliveredAt"] is not None:
            raise ValueError("prepared prompt carries a delivery time")
        if row["status"] in {"delivered", "answered", "completed"} and row["deliveredAt"] is None:
            raise ValueError("delivered prompt lacks a delivery time")
        if row["status"] in {"answered", "completed"} and row["answeredAt"] is None:
            raise ValueError("answered prompt lacks an answer time")
        active_prompts += row["status"] in {"prepared", "delivered", "answered"}
    if active_prompts > 1:
        raise ValueError("multiple active study prompts")
    _require_unique([row["promptKey"] for row in snapshot["studyPrompts"]], "duplicate prompt key")
    _require_unique(
        [row["queueItemId"] for row in snapshot["studyPrompts"]],
        "multiple prompts for one queue occurrence",
    )

    attempt_numbers: list[str] = []
    for row in snapshot["deliveryAttempts"]:
        if row["promptId"] not in prompt_ids:
            raise ValueError("orphan delivery attempt")
        if row["attemptNumber"] < 1:
            raise ValueError("invalid delivery attempt number")
        attempt_numbers.append(f"{row['promptId']}:{row['attemptNumber']}")
        if row["status"] == "delivered" and (
            row["receiptAt"] is None or row["outboundDeliveryId"] is None
        ):
            raise ValueError("delivered attempt lacks a receipt")
    _require_unique(attempt_numbers, "duplicate delivery attempt number")

    for row in snapshot["answerDrafts"]:
        if row["promptId"] not in prompt_ids:
            raise ValueError("orphan answer draft")
    _require_unique(
        [row["promptId"] for row in snapshot["answerDrafts"]], "multiple drafts for one prompt"
    )

    legacy_keys: list[str] = []
    for row in snapshot["reviewAttempts"]:
        if row["cardId"] not in card_ids:
            raise ValueError("orphan review attempt")
        for field, known in (
            ("sessionId", study_session_ids),
            ("queueItemId", queue_ids),
            ("promptId", prompt_ids),
            ("answerDraftId", draft_ids),
        ):
            if row[field] is not None and row[field] not in known:
                raise ValueError(f"review attempt references a missing {field}")
        if row["retryOfAttemptId"] is not None and (
            row["retryOfAttemptId"] == row["id"] or row["retryOfAttemptId"] not in attempt_ids
        ):
            raise ValueError("invalid review attempt retry link")
        if (row["legacySource"] is None) != (row["legacyId"] is None):
            raise ValueError("review attempt legacy identity is half-populated")
        if row["legacySource"] is not None:
            legacy_keys.append(f"{row['legacySource']}:{row['legacyId']}")
        if row["afterRepetitions"] != row["beforeRepetitions"] + 1:
            raise ValueError("review attempt repetitions do not advance by one")
        if row["afterLapses"] > row["afterRepetitions"] or row["afterRepetitions"] < 1:
            raise ValueError("review attempt lapse count out of range")
        if row["isSameSessionRetry"] not in (0, 1):
            raise ValueError("invalid same-session retry flag")
        if decode_real(row["afterStability"]) <= 0:
            raise ValueError("review attempt stability out of range")
        if not 1 <= decode_real(row["afterDifficulty"]) <= 10:
            raise ValueError("review attempt difficulty out of range")
        if not 0 < decode_real(row["desiredRetention"]) < 1:
            raise ValueError("review attempt desired retention out of range")
    _require_unique(legacy_keys, "duplicate migration attempt for one legacy row")
    _require_unique(
        [row["promptId"] for row in snapshot["reviewAttempts"]],
        "multiple final attempts for one prompt",
    )
    _require_unique(
        [row["retryOfAttemptId"] for row in snapshot["reviewAttempts"]],
        "duplicate review attempt retry",
    )


def validate_snapshot(value: Any) -> dict[str, Any]:
    expected = {"formatVersion", *(key for key, _, _ in TABLES)}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid snapshot shape")
    if value["formatVersion"] != FORMAT_VERSION:
        raise ValueError("unsupported snapshot format")
    root: dict[str, Any] = {"formatVersion": FORMAT_VERSION}
    for key, _, columns in TABLES:
        root[key] = _rows(value[key], columns)
    _validate_semantics(root)
    _validate_jcs_value(root)
    return root


def extract_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    snapshot: dict[str, Any] = {"formatVersion": FORMAT_VERSION}
    for key, table, columns in TABLES:
        selected = ", ".join(column[1] for column in columns)
        snapshot[key] = [
            {
                json_key: encode_real(row[sql_column])
                if kind in {"real", "realNull"} and row[sql_column] is not None
                else row[sql_column]
                for json_key, sql_column, kind, *_ in columns
            }
            for row in connection.execute(f"SELECT {selected} FROM {table} ORDER BY id")
        ]
    return validate_snapshot(snapshot)


def insert_snapshot(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    # study_queue.completed_attempt_id and review_attempts.queue_item_id form a
    # cycle, so no insert order satisfies both. validate_snapshot already proved
    # every reference resolves, leaving the commit-time check to confirm it.
    connection.execute("PRAGMA defer_foreign_keys = ON")
    for key, table, columns in TABLES:
        targets = ", ".join(column[1] for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO {table} ({targets}) VALUES ({placeholders})",
            (
                tuple(
                    decode_real(row[json_key])
                    if kind in {"real", "realNull"} and row[json_key] is not None
                    else row[json_key]
                    for json_key, _, kind, *_ in columns
                )
                for row in snapshot[key]
            ),
        )


def verify_database(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"database schema must be version {SCHEMA_VERSION}, found version {version}"
        )
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("database foreign-key check failed")
    extract_snapshot(connection)


def summary(snapshot: dict[str, Any], digest: str) -> dict[str, Any]:
    counts = {key: len(snapshot[key]) for key, _, _ in TABLES}
    return {**counts, "sha256": digest}


def max_ids(snapshot: dict[str, Any]) -> tuple[int | None, ...]:
    return tuple(
        snapshot[key][-1]["id"] if snapshot[key] else None for key, _, _ in TABLES
    )
