from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_vocab.capture import (
    MAX_SOURCE_CONTEXT_LENGTH,
    CaptureService,
    normalize_word,
)
from hermes_vocab.database import Database
from hermes_vocab.models import (
    CaptureCommand,
    CaptureOperation,
    CaptureStatus,
    SenseCard,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def service_for(tmp_path: Path) -> CaptureService:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    return CaptureService(database, clock=lambda: NOW)


def command(
    operation: CaptureOperation,
    *,
    word: str = "bank",
    definition: str = "A financial institution.",
    context: str | None = None,
    matching_sense_id: int | None = None,
    part_of_speech: str = "noun",
    example_sentence: str = "She visited the bank.",
) -> CaptureCommand:
    card = (
        None
        if operation is CaptureOperation.EXISTING_SENSE
        else SenseCard(
            part_of_speech=part_of_speech,
            definition=definition,
            example_sentence=example_sentence,
        )
    )
    return CaptureCommand(
        word=word,
        operation=operation,
        card=card,
        source_context=context,
        matching_sense_id=matching_sense_id,
    )


def test_new_word_creates_word_and_first_sense(tmp_path: Path) -> None:
    service = service_for(tmp_path)

    result = service.capture(command(CaptureOperation.NEW_WORD, context="At the bank."))

    assert result.status is CaptureStatus.SAVED
    assert result.word is not None
    assert result.word.normalized_word == "bank"
    assert result.word.date_added == NOW
    assert result.word.last_reviewed is None
    assert result.word.review_status == "new"
    assert len(result.word.senses) == 1
    assert result.sense == result.word.senses[0]
    assert result.sense.source_context == "At the bank."


def test_new_sense_preserves_word_review_fields_and_capture_order(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "vocabulary.sqlite3")
    database.initialize()
    timestamps = iter((NOW, NOW - timedelta(days=1)))
    service = CaptureService(database, clock=lambda: next(timestamps))
    original = service.capture(command(CaptureOperation.NEW_WORD)).word

    result = service.capture(
        command(
            CaptureOperation.NEW_SENSE,
            definition="Land alongside a river.",
            context="She sat on the bank and watched the river.",
        )
    )

    assert result.status is CaptureStatus.NEW_SENSE_SAVED
    assert result.word is not None
    assert [sense.definition for sense in result.word.senses] == [
        "A financial institution.",
        "Land alongside a river.",
    ]
    assert [sense.date_added for sense in result.word.senses] == [
        NOW,
        NOW - timedelta(days=1),
    ]
    assert service.get_word("bank").senses == result.word.senses
    assert result.sense == result.word.senses[1]
    assert result.sense.source_context == "She sat on the bank and watched the river."
    assert result.word.last_reviewed == original.last_reviewed
    assert result.word.review_status == original.review_status


def test_existing_sense_returns_match_without_write(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    first = service.capture(command(CaptureOperation.NEW_WORD))

    result = service.capture(
        command(
            CaptureOperation.EXISTING_SENSE,
            matching_sense_id=first.sense.id,
        )
    )

    assert result.status is CaptureStatus.ALREADY_EXISTS
    assert result.sense == first.sense
    assert result.word is not None
    assert len(result.word.senses) == 1
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0] == 1


def test_state_mismatches_return_conflict_with_current_word_when_available(
    tmp_path: Path,
) -> None:
    service = service_for(tmp_path)
    service.capture(command(CaptureOperation.NEW_WORD))

    duplicate_new = service.capture(command(CaptureOperation.NEW_WORD))
    missing_word = service.capture(
        command(
            CaptureOperation.NEW_SENSE,
            word="shore",
            definition="Land at the edge of water.",
        )
    )

    assert duplicate_new.status is CaptureStatus.CONFLICT
    assert duplicate_new.word is not None
    assert len(duplicate_new.word.senses) == 1
    assert missing_word.status is CaptureStatus.CONFLICT
    assert missing_word.word is None


def test_existing_sense_rejects_id_from_another_word(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    service.capture(command(CaptureOperation.NEW_WORD))
    shore = service.capture(
        command(
            CaptureOperation.NEW_WORD,
            word="shore",
            definition="Land at the edge of water.",
        )
    )

    result = service.capture(
        command(
            CaptureOperation.EXISTING_SENSE,
            matching_sense_id=shore.sense.id,
        )
    )

    assert result.status is CaptureStatus.CONFLICT
    assert result.word is not None
    assert len(result.word.senses) == 1
    assert result.sense is None


@pytest.mark.parametrize(
    "bad_command",
    [
        command(CaptureOperation.NEW_WORD, word="two words"),
        command(
            CaptureOperation.NEW_WORD,
            context="x" * (MAX_SOURCE_CONTEXT_LENGTH + 1),
        ),
        command(CaptureOperation.NEW_WORD, part_of_speech="x" * 51),
        command(CaptureOperation.NEW_WORD, definition="x" * 501),
        command(CaptureOperation.NEW_WORD, example_sentence="x" * 501),
        CaptureCommand("bank", CaptureOperation.NEW_WORD, None),
        CaptureCommand(
            "bank",
            CaptureOperation.NEW_WORD,
            SenseCard("noun", "definition", "example"),
            matching_sense_id=1,
        ),
        CaptureCommand(
            "bank",
            CaptureOperation.EXISTING_SENSE,
            SenseCard("noun", "definition", "example"),
            matching_sense_id=1,
        ),
        CaptureCommand("bank", CaptureOperation.EXISTING_SENSE, None),
    ],
)
def test_malformed_commands_are_invalid_without_writes(
    tmp_path: Path,
    bad_command: CaptureCommand,
) -> None:
    service = service_for(tmp_path)

    result = service.capture(bad_command)

    assert result.status is CaptureStatus.INVALID
    assert result.word is None
    assert result.sense is None
    assert service.get_word("bank") is None


@pytest.mark.parametrize("operation", ["new_word", "not_an_operation"])
def test_raw_string_operations_are_invalid_without_writes(
    tmp_path: Path,
    operation: str,
) -> None:
    service = service_for(tmp_path)
    raw_command = CaptureCommand(
        "bank",
        operation,
        SenseCard("noun", "A financial institution.", "She visited the bank."),
    )

    result = service.capture(raw_command)

    assert result.status is CaptureStatus.INVALID
    assert service.get_word("bank") is None


def test_field_boundaries_and_context_are_trimmed_and_persisted(tmp_path: Path) -> None:
    service = service_for(tmp_path)

    result = service.capture(
        command(
            CaptureOperation.NEW_WORD,
            word=f" {'w' * 100} ",
            part_of_speech=f" {'p' * 50} ",
            definition=f" {'d' * 500} ",
            example_sentence=f" {'e' * 500} ",
            context=f" {'c' * MAX_SOURCE_CONTEXT_LENGTH} ",
        )
    )

    assert result.status is CaptureStatus.SAVED
    assert result.word.word == "w" * 100
    assert result.sense.part_of_speech == "p" * 50
    assert result.sense.definition == "d" * 500
    assert result.sense.example_sentence == "e" * 500
    assert result.sense.source_context == "c" * MAX_SOURCE_CONTEXT_LENGTH


def test_unicode_lookup_normalizes_equivalent_words(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    saved = service.capture(command(CaptureOperation.NEW_WORD, word="Résumé"))

    assert normalize_word("  résumé  ") == "résumé"
    assert service.get_word("  résumé  ") == saved.word


def test_concurrent_new_word_creates_one_word_and_one_sense(tmp_path: Path) -> None:
    service = service_for(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: service.capture(command(CaptureOperation.NEW_WORD)),
                range(2),
            )
        )

    assert {result.status for result in results} == {
        CaptureStatus.SAVED,
        CaptureStatus.CONFLICT,
    }
    assert all(result.word is not None for result in results)
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_words").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM vocabulary_senses").fetchone()[0] == 1
