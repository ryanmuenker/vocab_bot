from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from hermes_vocab.database import Database
from hermes_vocab.cloudflare_snapshot import (
    canonical_bytes,
    decode_real,
    encode_real,
    extract_snapshot,
    insert_snapshot,
    load_envelope,
    max_ids,
    snapshot_sha256,
    summary,
    validate_snapshot,
    verify_database,
)

_IMPORT_SCRIPT = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts" / "import_cloudflare.py")
)
replace_database_files = _IMPORT_SCRIPT["replace_database_files"]

# Digest of the shared fixture below. worker/test/snapshot.test.ts asserts the
# same value against its byte-identical copy, which is what proves the two
# implementations agree on the wire format.
CROSS_LANGUAGE_DIGEST = "d4fa50222abf362c042a16b3402878990f994480f1c59f7128e88a91fbc4bba8"

_FINGERPRINT = "a" * 64
_CONTENT_FINGERPRINT = "b" * 64


def test_constrained_jcs_cross_language_vector() -> None:
    vector = {
        "nested": {"z": None, "a": True},
        "id": 9_007_199_254_740_991,
        "array": [
            "café",
            "Straße",
            "😀",
            "\u2028",
            '"',
            "\\",
            "\u0000\b\t\n\f\r\u001f",
        ],
    }
    assert hashlib.sha256(canonical_bytes(vector)).hexdigest() == (
        "f9aff60e240798e6b07e32bcfd2d38464a36e899642f167589c4a660752a61f6"
    )
    with pytest.raises(ValueError, match="surrogate"):
        canonical_bytes({"value": "\ud800"})


def test_reals_render_the_way_javascript_renders_them() -> None:
    assert encode_real(1.0) == "1.0"
    assert encode_real(10.0) == "10.0"
    assert encode_real(0.9) == "0.9"
    assert encode_real(3.2173) == "3.2173"
    assert encode_real(0.0001) == "0.0001"
    assert decode_real("3.2173") == 3.2173
    for outside in (1e-5, 1e16, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="outside the snapshot domain"):
            encode_real(outside)
    for malformed in ("1", "1.00", "0.90", "3.2173e0", "01.0", ""):
        with pytest.raises(ValueError, match="canonical decimal"):
            decode_real(malformed)


def _fixture() -> dict[str, object]:
    """Exercises every v5 table, both card directions, and preserved history."""
    return validate_snapshot(
        {
            "formatVersion": 2,
            "entries": [
                {
                    "id": 7,
                    "displayText": "Straße 😀",
                    "normalizedText": "strasse 😀",
                    "dateAdded": "2026-07-20T00:00:00Z",
                    "lastReviewed": "2026-07-22T16:30:00Z",
                    "reviewStatus": "reviewed",
                }
            ],
            "senses": [
                {
                    "id": 11,
                    "entryId": 7,
                    "definition": 'café "quote" \\ slash\u2028line',
                    "partOfSpeech": "noun",
                    "exampleSentence": "Control-safe example.",
                    "sourceContext": "source",
                    "dateAdded": "2026-07-20T00:00:00Z",
                },
                {
                    "id": 12,
                    "entryId": 7,
                    "definition": "a second sense",
                    "partOfSpeech": "verb",
                    "exampleSentence": "I define it.",
                    "sourceContext": None,
                    "dateAdded": "2026-07-20T00:00:00Z",
                },
            ],
            "reviewEvents": [
                {
                    "id": 3,
                    "entryId": 7,
                    "reviewDate": "2026-07-22",
                    "status": "answered",
                    "promptedAt": "2026-07-22T16:00:00Z",
                    "answeredAt": "2026-07-22T16:30:00Z",
                    "answerText": "an answer",
                    "grade": "partial",
                    "evaluationFeedback": "close enough",
                }
            ],
            "testSessions": [
                {
                    "id": 2,
                    "status": "completed",
                    "startedAt": "2026-07-21T17:00:00Z",
                    "completedAt": "2026-07-21T17:30:00Z",
                }
            ],
            "testQuestions": [
                {
                    "id": 5,
                    "sessionId": 2,
                    "entryId": 7,
                    "position": 1,
                    "answerText": "a test answer",
                    "grade": "correct",
                    "evaluationFeedback": "exactly right",
                    "answeredAt": "2026-07-21T17:20:00Z",
                }
            ],
            "cards": [
                {
                    "id": 20,
                    "entryId": 7,
                    "senseId": None,
                    "direction": "forward",
                    "state": "review",
                    "stability": "3.2173",
                    "difficulty": "5.0",
                    "dueAt": "2026-07-25T16:30:00Z",
                    "effectiveDueAt": "2026-07-26T00:00:00Z",
                    "lastReviewAt": "2026-07-22T16:30:00Z",
                    "repetitions": 1,
                    "lapses": 0,
                    "schedulerKind": "fsrs",
                    "schedulerVersion": "6",
                    "parametersVersion": "fsrs-6-default",
                    "parameterFingerprint": _FINGERPRINT,
                    "desiredRetention": "0.9",
                    "introducedLocalDate": "2026-07-20",
                    "buriedUntilLocalDate": None,
                    "createdAt": "2026-07-20T00:00:00Z",
                },
                {
                    "id": 21,
                    "entryId": 7,
                    "senseId": 11,
                    "direction": "reverse",
                    "state": "new",
                    "stability": None,
                    "difficulty": None,
                    "dueAt": "2026-07-20T00:00:00Z",
                    "effectiveDueAt": "2026-07-20T00:00:00Z",
                    "lastReviewAt": None,
                    "repetitions": 0,
                    "lapses": 0,
                    "schedulerKind": "fsrs",
                    "schedulerVersion": "6",
                    "parametersVersion": "fsrs-6-default",
                    "parameterFingerprint": _FINGERPRINT,
                    "desiredRetention": "0.9",
                    "introducedLocalDate": None,
                    "buriedUntilLocalDate": "2026-07-24",
                    "createdAt": "2026-07-20T00:00:00Z",
                },
            ],
            "studySessions": [
                {
                    "id": 30,
                    "mode": "review",
                    "status": "completed",
                    "startedAt": "2026-07-22T16:00:00Z",
                    "completedAt": "2026-07-22T16:35:00Z",
                    "localDate": "2026-07-22",
                    "legacyTestSessionId": 2,
                }
            ],
            "studyQueue": [
                {
                    "id": 40,
                    "sessionId": 30,
                    "cardId": 20,
                    "position": 1,
                    "status": "completed",
                    "retryOfQueueItemId": None,
                    "completedAttemptId": 60,
                    "legacyTestQuestionId": 5,
                    "introducedLocalDate": "2026-07-22",
                },
                {
                    "id": 41,
                    "sessionId": 30,
                    "cardId": 21,
                    "position": 2,
                    "status": "skipped",
                    "retryOfQueueItemId": 40,
                    "completedAttemptId": None,
                    "legacyTestQuestionId": None,
                    "introducedLocalDate": None,
                },
            ],
            "studyPrompts": [
                {
                    "id": 50,
                    "sessionId": 30,
                    "queueItemId": 40,
                    "promptKey": "review:30:40",
                    "promptText": "What does Straße mean?",
                    "status": "completed",
                    "preparedAt": "2026-07-22T16:00:00Z",
                    "deliveredAt": "2026-07-22T16:00:05Z",
                    "answeredAt": "2026-07-22T16:30:00Z",
                }
            ],
            "deliveryAttempts": [
                {
                    "id": 55,
                    "promptId": 50,
                    "attemptNumber": 1,
                    "status": "failed",
                    "attemptedAt": "2026-07-22T16:00:01Z",
                    "receiptAt": None,
                    "outboundDeliveryId": None,
                    "contentFingerprint": _CONTENT_FINGERPRINT,
                    "errorText": "429 Too Many Requests",
                },
                {
                    "id": 56,
                    "promptId": 50,
                    "attemptNumber": 2,
                    "status": "delivered",
                    "attemptedAt": "2026-07-22T16:00:04Z",
                    "receiptAt": "2026-07-22T16:00:05Z",
                    "outboundDeliveryId": "telegram:9182",
                    "contentFingerprint": _CONTENT_FINGERPRINT,
                    "errorText": None,
                },
            ],
            "answerDrafts": [
                {
                    "id": 58,
                    "promptId": 50,
                    "submittedAnswer": "a street",
                    "evaluatorGrade": "partial",
                    "evaluationFeedback": "close enough",
                    "answeredAt": "2026-07-22T16:30:00Z",
                    "createdAt": "2026-07-22T16:30:00Z",
                }
            ],
            "reviewAttempts": [
                {
                    "id": 60,
                    "cardId": 20,
                    "sessionId": 30,
                    "queueItemId": 40,
                    "promptId": 50,
                    "answerDraftId": 58,
                    "source": "review",
                    "rating": "hard",
                    "submittedAnswer": "a street",
                    "evaluatorGrade": "partial",
                    "evaluationFeedback": "close enough",
                    "reviewedAt": "2026-07-22T16:30:00Z",
                    "beforeState": "new",
                    "beforeStability": None,
                    "beforeDifficulty": None,
                    "beforeDueAt": "2026-07-20T00:00:00Z",
                    "beforeEffectiveDueAt": "2026-07-20T00:00:00Z",
                    "beforeLastReviewAt": None,
                    "beforeRepetitions": 0,
                    "beforeLapses": 0,
                    "afterState": "review",
                    "afterStability": "3.2173",
                    "afterDifficulty": "5.0",
                    "afterRawDueAt": "2026-07-25T16:30:00Z",
                    "afterEffectiveDueAt": "2026-07-26T00:00:00Z",
                    "afterLastReviewAt": "2026-07-22T16:30:00Z",
                    "afterRepetitions": 1,
                    "afterLapses": 0,
                    "schedulerKind": "fsrs",
                    "schedulerVersion": "6",
                    "parametersVersion": "fsrs-6-default",
                    "parameterFingerprint": _FINGERPRINT,
                    "desiredRetention": "0.9",
                    "isSameSessionRetry": 0,
                    "retryOfAttemptId": None,
                    "legacySource": None,
                    "legacyId": None,
                    "createdAt": "2026-07-22T16:30:00Z",
                },
                {
                    "id": 61,
                    "cardId": 21,
                    "sessionId": None,
                    "queueItemId": None,
                    "promptId": None,
                    "answerDraftId": None,
                    "source": "migration",
                    "rating": "again",
                    "submittedAnswer": None,
                    "evaluatorGrade": None,
                    "evaluationFeedback": None,
                    "reviewedAt": "2026-07-19T12:00:00Z",
                    "beforeState": "new",
                    "beforeStability": None,
                    "beforeDifficulty": None,
                    "beforeDueAt": "2026-07-19T12:00:00Z",
                    "beforeEffectiveDueAt": "2026-07-19T12:00:00Z",
                    "beforeLastReviewAt": None,
                    "beforeRepetitions": 0,
                    "beforeLapses": 0,
                    "afterState": "relearning",
                    "afterStability": "0.2172",
                    "afterDifficulty": "10.0",
                    "afterRawDueAt": "2026-07-19T12:10:00Z",
                    "afterEffectiveDueAt": "2026-07-20T00:00:00Z",
                    "afterLastReviewAt": "2026-07-19T12:00:00Z",
                    "afterRepetitions": 1,
                    "afterLapses": 1,
                    "schedulerKind": "fsrs",
                    "schedulerVersion": "6",
                    "parametersVersion": "fsrs-6-default",
                    "parameterFingerprint": _FINGERPRINT,
                    "desiredRetention": "0.9",
                    "isSameSessionRetry": 1,
                    "retryOfAttemptId": 60,
                    "legacySource": "review_events",
                    "legacyId": 3,
                    "createdAt": "2026-07-19T12:00:00Z",
                },
            ],
        }
    )


def _corrupt(**changes: object) -> dict[str, object]:
    """Return the fixture with one row shallow-patched, keyed `table.index`."""
    snapshot = json.loads(canonical_bytes(_fixture()))
    for path, value in changes.items():
        table, index, field = path.split("__")
        snapshot[table][int(index)][field] = value
    return snapshot


def test_snapshot_roundtrip_preserves_ids_state_and_bytes(tmp_path: Path) -> None:
    snapshot = _fixture()
    database_path = tmp_path / "source.sqlite3"
    Database(database_path).initialize()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        insert_snapshot(connection, snapshot)
        connection.commit()
        verify_database(connection)
        exported = extract_snapshot(connection)
    finally:
        connection.close()
    assert canonical_bytes(exported) == canonical_bytes(snapshot)
    assert max_ids(exported) == (7, 12, 3, 2, 5, 21, 30, 41, 50, 56, 58, 61)
    assert snapshot_sha256(exported) == CROSS_LANGUAGE_DIGEST
    assert json.loads(canonical_bytes(exported)) == snapshot
    assert summary(exported, CROSS_LANGUAGE_DIGEST) == {
        "entries": 1,
        "senses": 2,
        "reviewEvents": 1,
        "testSessions": 1,
        "testQuestions": 1,
        "cards": 2,
        "studySessions": 1,
        "studyQueue": 2,
        "studyPrompts": 1,
        "deliveryAttempts": 2,
        "answerDrafts": 1,
        "reviewAttempts": 2,
        "sha256": CROSS_LANGUAGE_DIGEST,
    }


def test_snapshot_envelope_rejects_a_digest_mismatch(tmp_path: Path) -> None:
    snapshot = _fixture()
    envelope = tmp_path / "export.json"

    envelope.write_text(
        json.dumps({"sha256": snapshot_sha256(snapshot), "snapshot": snapshot}),
        encoding="utf-8",
    )
    assert load_envelope(envelope)["sha256"] == CROSS_LANGUAGE_DIGEST

    tampered = _corrupt(entries__0__displayText="Strasse")
    envelope.write_text(
        json.dumps({"sha256": CROSS_LANGUAGE_DIGEST, "snapshot": tampered}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        load_envelope(envelope)


def test_snapshot_rejects_v1_format_and_broken_invariants() -> None:
    with pytest.raises(ValueError, match="unsupported snapshot format"):
        validate_snapshot({**_fixture(), "formatVersion": 1})
    with pytest.raises(ValueError, match="invalid snapshot shape"):
        validate_snapshot(
            {
                "formatVersion": 1,
                "entries": [],
                "senses": [],
                "reviewEvents": [],
                "testSessions": [],
                "testQuestions": [],
            }
        )
    for changes, message in (
        ({"cards__0__difficulty": "1.00"}, "canonical decimal"),
        ({"cards__1__senseId": 99}, "reverse card sense"),
        ({"cards__0__state": "new"}, "disagrees with its FSRS scalars"),
        ({"studyQueue__0__completedAttemptId": None}, "completion disagrees"),
        ({"reviewAttempts__0__afterRepetitions": 3}, "advance by one"),
        ({"studyPrompts__0__status": "prepared"}, "carries a delivery time"),
        ({"deliveryAttempts__1__receiptAt": None}, "lacks a receipt"),
        ({"answerDrafts__0__submittedAnswer": "  "}, "blank text"),
    ):
        with pytest.raises(ValueError, match=message):
            validate_snapshot(_corrupt(**changes))


def test_snapshot_export_rejects_a_v4_database(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        for name in (
            "001_initial.sql",
            "002_multi_sense.sql",
            "003_entry_terms.sql",
            "004_graded_reviews_and_tests.sql",
        ):
            connection.executescript(
                files("hermes_vocab.migrations").joinpath(name).read_text(encoding="utf-8")
            )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        with pytest.raises(ValueError, match="must be version 5, found version 4"):
            verify_database(connection)
    finally:
        connection.close()


def test_database_replacement_uses_collision_free_backups(tmp_path: Path) -> None:
    target = tmp_path / "vocabulary.sqlite3"
    target.write_bytes(b"original")
    first_fresh = tmp_path / "first.sqlite3"
    first_fresh.write_bytes(b"first")
    first_backup = replace_database_files(target, first_fresh)

    second_fresh = tmp_path / "second.sqlite3"
    second_fresh.write_bytes(b"second")
    second_backup = replace_database_files(target, second_fresh)

    assert first_backup is not None
    assert second_backup is not None
    assert first_backup != second_backup
    assert (first_backup / target.name).read_bytes() == b"original"
    assert (second_backup / target.name).read_bytes() == b"first"
    assert target.read_bytes() == b"second"


def test_database_replacement_restores_original_after_post_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "vocabulary.sqlite3"
    wal = Path(f"{target}-wal")
    shm = Path(f"{target}-shm")
    target.write_bytes(b"original")
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    fresh = tmp_path / "fresh.sqlite3"
    fresh.write_bytes(b"fresh")
    original_chmod = Path.chmod

    def fail_target_chmod(path: Path, mode: int) -> None:
        if path == target:
            raise OSError("chmod failed")
        original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_target_chmod)
    with pytest.raises(OSError, match="chmod failed"):
        replace_database_files(target, fresh)

    assert target.read_bytes() == b"original"
    assert wal.read_bytes() == b"wal"
    assert shm.read_bytes() == b"shm"
    assert list(tmp_path.glob("vocabulary.sqlite3.backup-*")) == []
