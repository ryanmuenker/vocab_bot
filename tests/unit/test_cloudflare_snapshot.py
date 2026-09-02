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
CROSS_LANGUAGE_DIGEST = "e98381d5a9c04b414326e7417d94d40ea68c5058bf77ae2293a0cbf52f22c21e"

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
    """Exercises every v6 table, both card directions, and preserved history."""
    return validate_snapshot(
        {
            "formatVersion": 3,
            "entries": [
                {
                    "id": 7,
                    "displayText": "Straße 😀",
                    "normalizedText": "strasse 😀",
                    "dateAdded": "2026-07-20T00:00:00Z",
                    "lastReviewed": "2026-07-22T16:30:00Z",
                    "reviewStatus": "reviewed",
                },
                {
                    "id": 8,
                    "displayText": "Fallback",
                    "normalizedText": "fallback",
                    "dateAdded": "2026-07-23T00:00:00Z",
                    "lastReviewed": None,
                    "reviewStatus": "new",
                },
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
                {
                    "id": 13,
                    "entryId": 8,
                    "definition": "a reserve entry without an image",
                    "partOfSpeech": "noun",
                    "exampleSentence": "The fallback remains textual.",
                    "sourceContext": None,
                    "dateAdded": "2026-07-23T00:00:00Z",
                },
            ],
            "vocabularyEntryImages": [
                {
                    "id": 70,
                    "entryId": 7,
                    "senseId": 11,
                    "category": "place",
                    "query": "Straße street Germany",
                    "description": "A street in Germany.",
                    "photoUrl": "https://upload.wikimedia.org/example.jpg",
                    "caption": "A street in Germany. Source: Example",
                    "sourceUrl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                    "telegramFileId": "AgACAgIAAxkBAAI",
                    "telegramFileUniqueId": "AQADexample",
                    "origin": "capture",
                    "createdAt": "2026-07-20T00:01:00Z",
                    "updatedAt": "2026-07-22T16:31:00Z",
                },
            ],
            "imageBackfillAttempts": [
                {
                    "id": 80,
                    "entryId": 8,
                    "status": "rate_limited",
                    "attemptCount": 2,
                    "lastError": "Wikimedia returned 429",
                    "attemptedAt": "2026-07-23T00:05:00Z",
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


def _legacy_fixture() -> dict[str, object]:
    snapshot = json.loads(canonical_bytes(_fixture()))
    snapshot["formatVersion"] = 2
    snapshot.pop("vocabularyEntryImages")
    snapshot.pop("imageBackfillAttempts")
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
    assert max_ids(exported) == (8, 13, 70, 80, 3, 2, 5, 21, 30, 41, 50, 56, 58, 61)
    assert snapshot_sha256(exported) == CROSS_LANGUAGE_DIGEST
    assert json.loads(canonical_bytes(exported)) == snapshot
    assert summary(exported, CROSS_LANGUAGE_DIGEST) == {
        "entries": 2,
        "senses": 3,
        "vocabularyEntryImages": 1,
        "imageBackfillAttempts": 1,
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


def test_v2_envelope_verifies_original_digest_before_upgrade(tmp_path: Path) -> None:
    legacy = _legacy_fixture()
    legacy_digest = snapshot_sha256(legacy)
    envelope = tmp_path / "legacy-export.json"
    envelope.write_text(
        json.dumps({"sha256": legacy_digest, "snapshot": legacy}),
        encoding="utf-8",
    )

    loaded = load_envelope(envelope)
    assert loaded["sha256"] == legacy_digest
    assert loaded["snapshot"] == {
        **legacy,
        "formatVersion": 3,
        "vocabularyEntryImages": [],
        "imageBackfillAttempts": [],
    }
    assert validate_snapshot(legacy) == loaded["snapshot"]

    envelope.write_text(
        json.dumps({"sha256": CROSS_LANGUAGE_DIGEST, "snapshot": legacy}),
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


def test_snapshot_rejects_malformed_image_states_and_orphans() -> None:
    for changes, message in (
        ({"vocabularyEntryImages__0__caption": None}, "metadata must be populated"),
        (
            {"vocabularyEntryImages__0__telegramFileUniqueId": None},
            "receipt must be populated",
        ),
        ({"vocabularyEntryImages__0__caption": "x" * 1025}, "caption exceeds"),
        ({"vocabularyEntryImages__0__entryId": 99}, "orphan vocabulary entry image"),
        ({"vocabularyEntryImages__0__senseId": 13}, "does not belong"),
        ({"imageBackfillAttempts__0__entryId": 99}, "orphan image backfill"),
        ({"imageBackfillAttempts__0__entryId": 7}, "cannot retain"),
        ({"imageBackfillAttempts__0__attemptCount": 0}, "must be positive"),
        ({"imageBackfillAttempts__0__lastError": None}, "disagrees with status"),
    ):
        with pytest.raises(ValueError, match=message):
            validate_snapshot(_corrupt(**changes))

    missing_photo = _corrupt(
        vocabularyEntryImages__0__photoUrl=None,
        vocabularyEntryImages__0__caption=None,
        vocabularyEntryImages__0__sourceUrl=None,
    )
    with pytest.raises(ValueError, match="receipt requires"):
        validate_snapshot(missing_photo)

    duplicate_image = _corrupt()
    duplicate_image["vocabularyEntryImages"].append(
        {
            **duplicate_image["vocabularyEntryImages"][0],
            "id": 71,
            "senseId": 12,
        }
    )
    with pytest.raises(ValueError, match="duplicate image association"):
        validate_snapshot(duplicate_image)

    duplicate_attempt = _corrupt()
    duplicate_attempt["imageBackfillAttempts"].append(
        {**duplicate_attempt["imageBackfillAttempts"][0], "id": 81}
    )
    with pytest.raises(ValueError, match="duplicate image backfill"):
        validate_snapshot(duplicate_attempt)

    malformed_shape = _corrupt()
    malformed_shape["vocabularyEntryImages"][0]["extra"] = "not canonical"
    with pytest.raises(ValueError, match="row shape"):
        validate_snapshot(malformed_shape)


def test_snapshot_accepts_intent_only_images_and_no_visual_attempts() -> None:
    intent_only = _corrupt(
        vocabularyEntryImages__0__photoUrl=None,
        vocabularyEntryImages__0__caption=None,
        vocabularyEntryImages__0__sourceUrl=None,
        vocabularyEntryImages__0__telegramFileId=None,
        vocabularyEntryImages__0__telegramFileUniqueId=None,
    )
    assert validate_snapshot(intent_only)["vocabularyEntryImages"][0]["photoUrl"] is None

    no_visual = _corrupt(
        imageBackfillAttempts__0__status="no_visual",
        imageBackfillAttempts__0__lastError=None,
    )
    assert validate_snapshot(no_visual)["imageBackfillAttempts"][0]["lastError"] is None


def test_snapshot_accepts_pre_grading_legacy_review_events() -> None:
    """v4 recorded answers before it recorded grades; those rows are real
    history and must survive the bridge rather than block the export."""
    ungraded = _corrupt(
        reviewEvents__0__grade=None,
        reviewEvents__0__evaluationFeedback=None,
    )
    validated = validate_snapshot(ungraded)
    assert validated["reviewEvents"][0]["grade"] is None
    assert validated["reviewEvents"][0]["evaluationFeedback"] is None

    # The answer itself still has to agree with the status in both directions.
    for changes, message in (
        ({"reviewEvents__0__answerText": None}, "disagree with status"),
        ({"reviewEvents__0__answeredAt": None}, "disagree with status"),
        ({"reviewEvents__0__status": "missed"}, "disagree with status"),
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
        with pytest.raises(ValueError, match="must be version 6, found version 4"):
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
