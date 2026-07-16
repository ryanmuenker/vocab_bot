from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from .database import Database
from .models import (
    CaptureCommand,
    CaptureOperation,
    CaptureRequest,
    CaptureResult,
    CaptureStatus,
    SenseCard,
    VocabularySense,
    VocabularyWord,
)

_JOINERS = {"-", "'", "’"}
_MAX_WORD = 100
_MAX_PART_OF_SPEECH = 50
_MAX_TEXT = 500
_MAX_CONTEXT = 2000


def normalize_word(word: str) -> str:
    return unicodedata.normalize("NFKC", word.strip()).casefold()


def is_lexical_word(word: str) -> bool:
    normalized = unicodedata.normalize("NFKC", word.strip())
    if not normalized or len(normalized) > _MAX_WORD:
        return False
    if not _is_letter(normalized[0]) or not _is_letter(normalized[-1]):
        return False
    return all(_is_letter(character) or character in _JOINERS for character in normalized)


def parse_capture_message(message: str) -> CaptureRequest | None:
    stripped = message.strip()
    if not stripped or stripped.startswith("/"):
        return None
    lines = stripped.splitlines()
    word = lines[0].strip()
    if not is_lexical_word(word):
        return None
    context = "\n".join(lines[1:]).strip() or None
    return CaptureRequest(word=word, context=context)


def _is_letter(character: str) -> bool:
    return unicodedata.category(character).startswith("L")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sense_from_row(row: sqlite3.Row) -> VocabularySense:
    date_added = _parse_timestamp(row["date_added"])
    assert date_added is not None
    return VocabularySense(
        id=row["id"],
        word_id=row["word_id"],
        definition=row["definition"],
        part_of_speech=row["part_of_speech"],
        example_sentence=row["example_sentence"],
        source_context=row["source_context"],
        date_added=date_added,
    )


def _word_from_rows(
    word_row: sqlite3.Row,
    sense_rows: Sequence[sqlite3.Row],
) -> VocabularyWord:
    date_added = _parse_timestamp(word_row["date_added"])
    assert date_added is not None
    return VocabularyWord(
        id=word_row["id"],
        word=word_row["word"],
        normalized_word=word_row["normalized_word"],
        date_added=date_added,
        last_reviewed=_parse_timestamp(word_row["last_reviewed"]),
        review_status=word_row["review_status"],
        senses=tuple(_sense_from_row(row) for row in sense_rows),
    )


def _load_word(
    connection: sqlite3.Connection,
    normalized_word: str,
) -> VocabularyWord | None:
    word_row = connection.execute(
        "SELECT * FROM vocabulary_words WHERE normalized_word = ?",
        (normalized_word,),
    ).fetchone()
    if word_row is None:
        return None
    sense_rows = connection.execute(
        "SELECT * FROM vocabulary_senses WHERE word_id = ? ORDER BY date_added, id",
        (word_row["id"],),
    ).fetchall()
    return _word_from_rows(word_row, sense_rows)


class CaptureService:
    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.clock = clock

    def get_word(self, word: str) -> VocabularyWord | None:
        normalized_word = normalize_word(word)
        with self.database.connect() as connection:
            return _load_word(connection, normalized_word)

    def capture(self, command: CaptureCommand) -> CaptureResult:
        prepared = self._prepare_command(command)
        if prepared is None:
            return CaptureResult(CaptureStatus.INVALID)
        word, normalized_word, card, source_context = prepared

        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_word(connection, normalized_word)

                if command.operation is CaptureOperation.NEW_WORD:
                    if current is not None:
                        connection.commit()
                        return CaptureResult(CaptureStatus.CONFLICT, current)
                    timestamp = _timestamp(self.clock())
                    cursor = connection.execute(
                        """
                        INSERT INTO vocabulary_words (
                            word, normalized_word, date_added, review_status
                        ) VALUES (?, ?, ?, 'new')
                        """,
                        (word, normalized_word, timestamp),
                    )
                    word_id = cursor.lastrowid
                    sense_id = self._insert_sense(
                        connection,
                        word_id,
                        card,
                        source_context,
                        timestamp,
                    )
                    status = CaptureStatus.SAVED

                elif command.operation is CaptureOperation.NEW_SENSE:
                    if current is None:
                        connection.commit()
                        return CaptureResult(CaptureStatus.CONFLICT)
                    timestamp = _timestamp(self.clock())
                    word_id = current.id
                    sense_id = self._insert_sense(
                        connection,
                        word_id,
                        card,
                        source_context,
                        timestamp,
                    )
                    status = CaptureStatus.NEW_SENSE_SAVED

                else:
                    if current is None:
                        connection.commit()
                        return CaptureResult(CaptureStatus.CONFLICT)
                    matched = next(
                        (
                            sense
                            for sense in current.senses
                            if sense.id == command.matching_sense_id
                        ),
                        None,
                    )
                    if matched is None:
                        connection.commit()
                        return CaptureResult(CaptureStatus.CONFLICT, current)
                    connection.commit()
                    return CaptureResult(
                        CaptureStatus.ALREADY_EXISTS,
                        current,
                        matched,
                    )

                aggregate = _load_word(connection, normalized_word)
                assert aggregate is not None
                created = next(sense for sense in aggregate.senses if sense.id == sense_id)
                connection.commit()
                return CaptureResult(status, aggregate, created)
        except sqlite3.Error:
            return CaptureResult(CaptureStatus.STORAGE_ERROR)

    @staticmethod
    def _insert_sense(
        connection: sqlite3.Connection,
        word_id: int,
        card: SenseCard,
        source_context: str | None,
        timestamp: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO vocabulary_senses (
                word_id, definition, part_of_speech, example_sentence,
                source_context, date_added
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                word_id,
                card.definition,
                card.part_of_speech,
                card.example_sentence,
                source_context,
                timestamp,
            ),
        )
        return cursor.lastrowid

    @staticmethod
    def _prepare_command(
        command: CaptureCommand,
    ) -> tuple[str, str, SenseCard, str | None] | None:
        if not is_lexical_word(command.word):
            return None
        word = unicodedata.normalize("NFKC", command.word.strip())
        normalized_word = normalize_word(word)
        source_context = (
            command.source_context.strip() if command.source_context is not None else None
        )
        source_context = source_context or None
        if source_context is not None and len(source_context) > _MAX_CONTEXT:
            return None

        if command.operation is CaptureOperation.EXISTING_SENSE:
            if command.card is not None or command.matching_sense_id is None:
                return None
            return word, normalized_word, SenseCard("", "", ""), source_context

        if command.operation not in (
            CaptureOperation.NEW_WORD,
            CaptureOperation.NEW_SENSE,
        ):
            return None
        if command.card is None or command.matching_sense_id is not None:
            return None
        part_of_speech = command.card.part_of_speech.strip()
        definition = command.card.definition.strip()
        example_sentence = command.card.example_sentence.strip()
        if not (
            0 < len(part_of_speech) <= _MAX_PART_OF_SPEECH
            and 0 < len(definition) <= _MAX_TEXT
            and 0 < len(example_sentence) <= _MAX_TEXT
        ):
            return None
        return (
            word,
            normalized_word,
            SenseCard(part_of_speech, definition, example_sentence),
            source_context,
        )
