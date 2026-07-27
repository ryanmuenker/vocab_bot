from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_SAFE_INTEGER = 9_007_199_254_740_991
TABLES = (
    "vocabulary_entries",
    "vocabulary_senses",
    "review_events",
    "test_sessions",
    "test_questions",
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


def _exact(row: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != keys:
        raise ValueError("invalid snapshot row shape")
    return row


def _identifier(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SAFE_INTEGER:
        raise ValueError("invalid snapshot identifier")
    return value


def _text(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("invalid snapshot text")
    _validate_unicode(value)
    return value


def _rows(value: Any, keys: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("snapshot table must be an array")
    rows = [_exact(row, keys) for row in value]
    ids = [_identifier(row["id"]) for row in rows]
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError("snapshot rows must have unique ascending IDs")
    return rows


def validate_snapshot(value: Any) -> dict[str, Any]:
    root = _exact(
        value,
        {
            "formatVersion",
            "entries",
            "senses",
            "reviewEvents",
            "testSessions",
            "testQuestions",
        },
    )
    if root["formatVersion"] != 1:
        raise ValueError("unsupported snapshot format")
    entries = _rows(
        root["entries"],
        {"id", "displayText", "normalizedText", "dateAdded", "lastReviewed", "reviewStatus"},
    )
    senses = _rows(
        root["senses"],
        {"id", "entryId", "definition", "partOfSpeech", "exampleSentence", "sourceContext", "dateAdded"},
    )
    reviews = _rows(
        root["reviewEvents"],
        {"id", "entryId", "reviewDate", "status", "promptedAt", "answeredAt", "answerText", "grade", "evaluationFeedback"},
    )
    sessions = _rows(root["testSessions"], {"id", "status", "startedAt", "completedAt"})
    questions = _rows(
        root["testQuestions"],
        {"id", "sessionId", "entryId", "position", "answerText", "grade", "evaluationFeedback", "answeredAt"},
    )
    entry_ids = {_identifier(row["id"]) for row in entries}
    session_ids = {_identifier(row["id"]) for row in sessions}
    normalized: set[str] = set()
    for row in entries:
        _text(row["displayText"])
        identity = _text(row["normalizedText"])
        if identity in normalized:
            raise ValueError("duplicate normalized entry")
        normalized.add(identity)
        _text(row["dateAdded"])
        _text(row["lastReviewed"], nullable=True)
        if row["reviewStatus"] not in {"new", "reviewed"}:
            raise ValueError("invalid review status")
    for row in senses:
        if _identifier(row["entryId"]) not in entry_ids:
            raise ValueError("orphan vocabulary sense")
        for key in ("definition", "partOfSpeech", "exampleSentence", "dateAdded"):
            _text(row[key])
        _text(row["sourceContext"], nullable=True)
    review_dates: set[str] = set()
    for row in reviews:
        if _identifier(row["entryId"]) not in entry_ids:
            raise ValueError("orphan review event")
        review_date = _text(row["reviewDate"])
        if review_date in review_dates:
            raise ValueError("duplicate review date")
        review_dates.add(review_date)
        _text(row["promptedAt"])
        answered_at = _text(row["answeredAt"], nullable=True)
        answer = _text(row["answerText"], nullable=True)
        feedback = _text(row["evaluationFeedback"], nullable=True)
        grade = row["grade"]
        status = row["status"]
        if status not in {"pending", "answered", "missed"} or grade not in {
            None,
            "correct",
            "partial",
            "incorrect",
        }:
            raise ValueError("invalid review event")
        if status == "answered":
            if None in (answered_at, answer, grade, feedback):
                raise ValueError("incomplete answered review")
        elif any(item is not None for item in (answered_at, answer, grade, feedback)):
            raise ValueError("non-answered review has answer fields")
    active = 0
    for row in sessions:
        status = row["status"]
        _text(row["startedAt"])
        completed = _text(row["completedAt"], nullable=True)
        if status == "active":
            active += 1
            if completed is not None:
                raise ValueError("active test has completion time")
        elif status != "completed" or completed is None:
            raise ValueError("invalid test session")
    if active > 1:
        raise ValueError("multiple active tests")
    positions: set[tuple[int, int]] = set()
    entries_per_session: set[tuple[int, int]] = set()
    for row in questions:
        session_id = _identifier(row["sessionId"])
        entry_id = _identifier(row["entryId"])
        position = _identifier(row["position"])
        if session_id not in session_ids or entry_id not in entry_ids or not 1 <= position <= 5:
            raise ValueError("invalid test question relationship")
        if (session_id, position) in positions or (session_id, entry_id) in entries_per_session:
            raise ValueError("duplicate test question identity")
        positions.add((session_id, position))
        entries_per_session.add((session_id, entry_id))
        answer = _text(row["answerText"], nullable=True)
        feedback = _text(row["evaluationFeedback"], nullable=True)
        answered_at = _text(row["answeredAt"], nullable=True)
        grade = row["grade"]
        if grade not in {None, "correct", "partial", "incorrect"}:
            raise ValueError("invalid test grade")
        pending = all(item is None for item in (answer, grade, feedback, answered_at))
        answered = all(item is not None for item in (answer, grade, feedback, answered_at))
        if not pending and not answered:
            raise ValueError("partial test answer")
    _validate_jcs_value(root)
    return root


def extract_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    entries = [
        {
            "id": row["id"],
            "displayText": row["display_text"],
            "normalizedText": row["normalized_text"],
            "dateAdded": row["date_added"],
            "lastReviewed": row["last_reviewed"],
            "reviewStatus": row["review_status"],
        }
        for row in connection.execute("SELECT * FROM vocabulary_entries ORDER BY id")
    ]
    senses = [
        {
            "id": row["id"],
            "entryId": row["entry_id"],
            "definition": row["definition"],
            "partOfSpeech": row["part_of_speech"],
            "exampleSentence": row["example_sentence"],
            "sourceContext": row["source_context"],
            "dateAdded": row["date_added"],
        }
        for row in connection.execute("SELECT * FROM vocabulary_senses ORDER BY id")
    ]
    reviews = [
        {
            "id": row["id"],
            "entryId": row["entry_id"],
            "reviewDate": row["review_date"],
            "status": row["status"],
            "promptedAt": row["prompted_at"],
            "answeredAt": row["answered_at"],
            "answerText": row["answer_text"],
            "grade": row["grade"],
            "evaluationFeedback": row["evaluation_feedback"],
        }
        for row in connection.execute("SELECT * FROM review_events ORDER BY id")
    ]
    sessions = [
        {
            "id": row["id"],
            "status": row["status"],
            "startedAt": row["started_at"],
            "completedAt": row["completed_at"],
        }
        for row in connection.execute("SELECT * FROM test_sessions ORDER BY id")
    ]
    questions = [
        {
            "id": row["id"],
            "sessionId": row["session_id"],
            "entryId": row["entry_id"],
            "position": row["position"],
            "answerText": row["answer_text"],
            "grade": row["grade"],
            "evaluationFeedback": row["evaluation_feedback"],
            "answeredAt": row["answered_at"],
        }
        for row in connection.execute("SELECT * FROM test_questions ORDER BY id")
    ]
    return validate_snapshot(
        {
            "formatVersion": 1,
            "entries": entries,
            "senses": senses,
            "reviewEvents": reviews,
            "testSessions": sessions,
            "testQuestions": questions,
        }
    )


def insert_snapshot(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    connection.executemany(
        "INSERT INTO vocabulary_entries (id, display_text, normalized_text, date_added, last_reviewed, review_status) VALUES (?, ?, ?, ?, ?, ?)",
        ((row["id"], row["displayText"], row["normalizedText"], row["dateAdded"], row["lastReviewed"], row["reviewStatus"]) for row in snapshot["entries"]),
    )
    connection.executemany(
        "INSERT INTO vocabulary_senses (id, entry_id, definition, part_of_speech, example_sentence, source_context, date_added) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ((row["id"], row["entryId"], row["definition"], row["partOfSpeech"], row["exampleSentence"], row["sourceContext"], row["dateAdded"]) for row in snapshot["senses"]),
    )
    connection.executemany(
        "INSERT INTO review_events (id, entry_id, review_date, status, prompted_at, answered_at, answer_text, grade, evaluation_feedback) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ((row["id"], row["entryId"], row["reviewDate"], row["status"], row["promptedAt"], row["answeredAt"], row["answerText"], row["grade"], row["evaluationFeedback"]) for row in snapshot["reviewEvents"]),
    )
    connection.executemany(
        "INSERT INTO test_sessions (id, status, started_at, completed_at) VALUES (?, ?, ?, ?)",
        ((row["id"], row["status"], row["startedAt"], row["completedAt"]) for row in snapshot["testSessions"]),
    )
    connection.executemany(
        "INSERT INTO test_questions (id, session_id, entry_id, position, answer_text, grade, evaluation_feedback, answered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ((row["id"], row["sessionId"], row["entryId"], row["position"], row["answerText"], row["grade"], row["evaluationFeedback"], row["answeredAt"]) for row in snapshot["testQuestions"]),
    )


def verify_database(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != 4:
        raise ValueError("database schema must be version 4")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("database foreign-key check failed")
    extract_snapshot(connection)


def summary(snapshot: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "entries": len(snapshot["entries"]),
        "senses": len(snapshot["senses"]),
        "reviewEvents": len(snapshot["reviewEvents"]),
        "testSessions": len(snapshot["testSessions"]),
        "testQuestions": len(snapshot["testQuestions"]),
        "sha256": digest,
    }


def max_ids(snapshot: dict[str, Any]) -> tuple[int | None, ...]:
    arrays: Iterable[list[dict[str, Any]]] = (
        snapshot["entries"],
        snapshot["senses"],
        snapshot["reviewEvents"],
        snapshot["testSessions"],
        snapshot["testQuestions"],
    )
    return tuple(rows[-1]["id"] if rows else None for rows in arrays)
