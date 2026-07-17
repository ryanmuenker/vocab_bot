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
    EntryTextStatus,
    EntryCaptureResult,
    NormalizedEntryText,
    SenseCard,
    VocabularyEntry,
    VocabularySense,
)

_MAX_ENTRY_TEXT = 500
MAX_PART_OF_SPEECH_LENGTH = 50
MAX_SENSE_TEXT_LENGTH = 500
MAX_SOURCE_CONTEXT_LENGTH = 2000


def normalize_entry_text(text: str) -> NormalizedEntryText:
    display_text = unicodedata.normalize("NFKC", text).strip()
    if not display_text:
        return NormalizedEntryText(EntryTextStatus.EMPTY)
    if len(display_text) > _MAX_ENTRY_TEXT:
        return NormalizedEntryText(EntryTextStatus.TOO_LONG)
    normalized_text = " ".join(display_text.split()).casefold()
    return NormalizedEntryText(
        EntryTextStatus.VALID,
        display_text=display_text,
        normalized_text=normalized_text,
    )

def normalize_sense_identity(
    part_of_speech: str,
    definition: str,
) -> tuple[str, str]:
    return (
        " ".join(unicodedata.normalize("NFKC", part_of_speech).split()).casefold(),
        " ".join(unicodedata.normalize("NFKC", definition).split()).casefold(),
    )



def parse_capture_message(message: str) -> CaptureRequest | None:
    stripped = message.strip()
    if not stripped or stripped.startswith("/"):
        return None
    lines = stripped.splitlines()
    normalized = normalize_entry_text(lines[0])
    if normalized.status is not EntryTextStatus.VALID:
        return None
    context = "\n".join(lines[1:]).strip() or None
    return CaptureRequest(display_text=normalized.display_text, context=context)


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
        entry_id=row["entry_id"],
        definition=row["definition"],
        part_of_speech=row["part_of_speech"],
        example_sentence=row["example_sentence"],
        source_context=row["source_context"],
        date_added=date_added,
    )


def _entry_from_rows(
    entry_row: sqlite3.Row,
    sense_rows: Sequence[sqlite3.Row],
) -> VocabularyEntry:
    date_added = _parse_timestamp(entry_row["date_added"])
    assert date_added is not None
    return VocabularyEntry(
        id=entry_row["id"],
        display_text=entry_row["display_text"],
        normalized_text=entry_row["normalized_text"],
        date_added=date_added,
        last_reviewed=_parse_timestamp(entry_row["last_reviewed"]),
        review_status=entry_row["review_status"],
        senses=tuple(_sense_from_row(row) for row in sense_rows),
    )


def _load_entry(
    connection: sqlite3.Connection,
    normalized_text: str,
) -> VocabularyEntry | None:
    entry_row = connection.execute(
        "SELECT * FROM vocabulary_entries WHERE normalized_text = ?",
        (normalized_text,),
    ).fetchone()
    if entry_row is None:
        return None
    sense_rows = connection.execute(
        """
        SELECT * FROM vocabulary_senses
        WHERE entry_id = ?
        ORDER BY id
        """,
        (entry_row["id"],),
    ).fetchall()
    return _entry_from_rows(entry_row, sense_rows)


class CaptureService:
    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.clock = clock

    def get_entry(self, text: str) -> VocabularyEntry | None:
        normalized = normalize_entry_text(text)
        if normalized.status is not EntryTextStatus.VALID:
            return None
        with self.database.connect() as connection:
            return _load_entry(connection, normalized.normalized_text)

    def capture_entry(
        self,
        display_text: str,
        cards: Sequence[SenseCard],
    ) -> EntryCaptureResult:
        try:
            batch = tuple(cards)
        except TypeError:
            return EntryCaptureResult(CaptureStatus.INVALID)
        normalized = normalize_entry_text(display_text)
        prepared_cards = self._prepare_cards(batch)
        if (
            normalized.status is not EntryTextStatus.VALID
            or prepared_cards is None
        ):
            return EntryCaptureResult(CaptureStatus.INVALID)

        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = _load_entry(connection, normalized.normalized_text)
                if existing is not None:
                    connection.rollback()
                    return EntryCaptureResult(CaptureStatus.ALREADY_EXISTS, existing)

                timestamp = _timestamp(self.clock())
                cursor = connection.execute(
                    """
                    INSERT INTO vocabulary_entries (
                        display_text, normalized_text, date_added, review_status
                    ) VALUES (?, ?, ?, 'new')
                    """,
                    (
                        normalized.display_text,
                        normalized.normalized_text,
                        timestamp,
                    ),
                )
                entry_id = int(cursor.lastrowid)
                for card in prepared_cards:
                    self._insert_batch_sense(
                        connection,
                        entry_id,
                        card,
                        timestamp,
                    )

                entry = _load_entry(connection, normalized.normalized_text)
                if entry is None or len(entry.senses) != len(prepared_cards):
                    raise sqlite3.DatabaseError("incomplete capture aggregate")
                connection.commit()
                return EntryCaptureResult(CaptureStatus.SAVED, entry)
        except sqlite3.Error:
            return EntryCaptureResult(CaptureStatus.STORAGE_ERROR)

    def capture(self, command: CaptureCommand) -> CaptureResult:
        prepared = self._prepare_command(command)
        if prepared is None:
            return CaptureResult(CaptureStatus.INVALID)
        display_text, normalized_text, card, source_context = prepared

        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_entry(connection, normalized_text)

                if command.operation is CaptureOperation.NEW_ENTRY:
                    if current is not None:
                        connection.commit()
                        return CaptureResult(CaptureStatus.CONFLICT, current)
                    timestamp = _timestamp(self.clock())
                    cursor = connection.execute(
                        """
                        INSERT INTO vocabulary_entries (
                            display_text, normalized_text, date_added, review_status
                        ) VALUES (?, ?, ?, 'new')
                        """,
                        (display_text, normalized_text, timestamp),
                    )
                    entry_id = cursor.lastrowid
                    sense_id = self._insert_sense(
                        connection,
                        entry_id,
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
                    entry_id = current.id
                    sense_id = self._insert_sense(
                        connection,
                        entry_id,
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

                aggregate = _load_entry(connection, normalized_text)
                assert aggregate is not None
                created = next(sense for sense in aggregate.senses if sense.id == sense_id)
                connection.commit()
                return CaptureResult(status, aggregate, created)
        except sqlite3.Error:
            return CaptureResult(CaptureStatus.STORAGE_ERROR)

    @staticmethod
    def _insert_sense(
        connection: sqlite3.Connection,
        entry_id: int,
        card: SenseCard,
        source_context: str | None,
        timestamp: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO vocabulary_senses (
                entry_id, definition, part_of_speech, example_sentence,
                source_context, date_added
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                card.definition,
                card.part_of_speech,
                card.example_sentence,
                source_context,
                timestamp,
            ),
        )
        return cursor.lastrowid

    @staticmethod
    def _insert_batch_sense(
        connection: sqlite3.Connection,
        entry_id: int,
        card: SenseCard,
        timestamp: str,
    ) -> int:
        return CaptureService._insert_sense(
            connection,
            entry_id,
            card,
            None,
            timestamp,
        )

    @staticmethod
    def _prepare_cards(cards: tuple[SenseCard, ...]) -> tuple[SenseCard, ...] | None:
        if not 1 <= len(cards) <= 20:
            return None
        prepared: list[SenseCard] = []
        seen: set[tuple[str, str]] = set()
        for card in cards:
            if not isinstance(card, SenseCard) or not all(
                isinstance(field, str)
                for field in (
                    card.part_of_speech,
                    card.definition,
                    card.example_sentence,
                )
            ):
                return None
            part_of_speech = card.part_of_speech.strip()
            definition = card.definition.strip()
            example_sentence = card.example_sentence.strip()
            if not (
                0 < len(part_of_speech) <= MAX_PART_OF_SPEECH_LENGTH
                and 0 < len(definition) <= MAX_SENSE_TEXT_LENGTH
                and 0 < len(example_sentence) <= MAX_SENSE_TEXT_LENGTH
            ):
                return None
            key = normalize_sense_identity(part_of_speech, definition)
            if key in seen:
                return None
            seen.add(key)
            prepared.append(SenseCard(part_of_speech, definition, example_sentence))
        return tuple(prepared)

    @staticmethod
    def _prepare_command(
        command: CaptureCommand,
    ) -> tuple[str, str, SenseCard, str | None] | None:
        if not isinstance(command.operation, CaptureOperation):
            return None
        normalized = normalize_entry_text(command.display_text)
        if normalized.status is not EntryTextStatus.VALID:
            return None
        source_context = (
            command.source_context.strip() if command.source_context is not None else None
        )
        source_context = source_context or None
        if (
            source_context is not None
            and len(source_context) > MAX_SOURCE_CONTEXT_LENGTH
        ):
            return None

        if command.operation is CaptureOperation.EXISTING_SENSE:
            if command.card is not None or command.matching_sense_id is None:
                return None
            return (
                normalized.display_text,
                normalized.normalized_text,
                SenseCard("", "", ""),
                source_context,
            )

        if command.operation not in (
            CaptureOperation.NEW_ENTRY,
            CaptureOperation.NEW_SENSE,
        ):
            return None
        if command.card is None or command.matching_sense_id is not None:
            return None
        part_of_speech = command.card.part_of_speech.strip()
        definition = command.card.definition.strip()
        example_sentence = command.card.example_sentence.strip()
        if not (
            0 < len(part_of_speech) <= MAX_PART_OF_SPEECH_LENGTH
            and 0 < len(definition) <= MAX_SENSE_TEXT_LENGTH
            and 0 < len(example_sentence) <= MAX_SENSE_TEXT_LENGTH
        ):
            return None
        return (
            normalized.display_text,
            normalized.normalized_text,
            SenseCard(part_of_speech, definition, example_sentence),
            source_context,
        )
