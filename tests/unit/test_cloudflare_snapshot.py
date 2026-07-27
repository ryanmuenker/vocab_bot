from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
from pathlib import Path

import pytest

from hermes_vocab.database import Database
from hermes_vocab.cloudflare_snapshot import (
    canonical_bytes,
    extract_snapshot,
    insert_snapshot,
    max_ids,
    snapshot_sha256,
    validate_snapshot,
    verify_database,
)

_IMPORT_SCRIPT = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts" / "import_cloudflare.py")
)
replace_database_files = _IMPORT_SCRIPT["replace_database_files"]


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


def _fixture() -> dict[str, object]:
    return validate_snapshot(
        {
            "formatVersion": 1,
            "entries": [
                {
                    "id": 7,
                    "displayText": "Straße 😀",
                    "normalizedText": "strasse 😀",
                    "dateAdded": "2026-07-20T00:00:00Z",
                    "lastReviewed": None,
                    "reviewStatus": "new",
                }
            ],
            "senses": [
                {
                    "id": 11,
                    "entryId": 7,
                    "definition": "café",
                    "partOfSpeech": "noun",
                    "exampleSentence": "A quoted \\\"example\\\".",
                    "sourceContext": "source\u2028context",
                    "dateAdded": "2026-07-20T00:00:00Z",
                }
            ],
            "reviewEvents": [
                {
                    "id": 3,
                    "entryId": 7,
                    "reviewDate": "2026-07-23",
                    "status": "pending",
                    "promptedAt": "2026-07-22T16:00:00Z",
                    "answeredAt": None,
                    "answerText": None,
                    "grade": None,
                    "evaluationFeedback": None,
                }
            ],
            "testSessions": [
                {
                    "id": 2,
                    "status": "active",
                    "startedAt": "2026-07-22T17:00:00Z",
                    "completedAt": None,
                }
            ],
            "testQuestions": [
                {
                    "id": 5,
                    "sessionId": 2,
                    "entryId": 7,
                    "position": 1,
                    "answerText": None,
                    "grade": None,
                    "evaluationFeedback": None,
                    "answeredAt": None,
                }
            ],
        }
    )


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
    assert max_ids(exported) == (7, 11, 3, 2, 5)
    assert snapshot_sha256(exported) == snapshot_sha256(snapshot)
    assert json.loads(canonical_bytes(exported)) == snapshot


def test_snapshot_export_rejects_non_v4_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "source.sqlite3"
    Database(database_path).initialize()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA user_version = 5")
        with pytest.raises(ValueError, match="version 4"):
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
