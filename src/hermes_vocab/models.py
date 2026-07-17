from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class CaptureOperation(StrEnum):
    NEW_ENTRY = "new_entry"
    NEW_SENSE = "new_sense"
    EXISTING_SENSE = "existing_sense"


class CaptureStatus(StrEnum):
    SAVED = "saved"
    NEW_SENSE_SAVED = "new_sense_saved"
    ALREADY_EXISTS = "already_exists"
    INVALID = "invalid"
    CONFLICT = "conflict"
    STORAGE_ERROR = "storage_error"


class EntryTextStatus(StrEnum):
    VALID = "valid"
    EMPTY = "empty"
    TOO_LONG = "too_long"


@dataclass(frozen=True, slots=True)
class NormalizedEntryText:
    status: EntryTextStatus
    display_text: str | None = None
    normalized_text: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    display_text: str
    context: str | None


@dataclass(frozen=True, slots=True)
class SenseCard:
    part_of_speech: str
    definition: str
    example_sentence: str


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    display_text: str
    operation: CaptureOperation
    card: SenseCard | None = None
    source_context: str | None = None
    matching_sense_id: int | None = None


@dataclass(frozen=True, slots=True)
class VocabularySense:
    id: int
    entry_id: int
    definition: str
    part_of_speech: str
    example_sentence: str
    source_context: str | None
    date_added: datetime


@dataclass(frozen=True, slots=True)
class VocabularyEntry:
    id: int
    display_text: str
    normalized_text: str
    date_added: datetime
    last_reviewed: datetime | None
    review_status: str
    senses: tuple[VocabularySense, ...]


@dataclass(frozen=True, slots=True)
class CaptureResult:
    status: CaptureStatus
    entry: VocabularyEntry | None = None
    sense: VocabularySense | None = None
@dataclass(frozen=True, slots=True)
class EntryCaptureResult:
    status: CaptureStatus
    entry: VocabularyEntry | None = None




class PendingReviewStatus(StrEnum):
    PENDING = "pending"
    NONE = "none"
    STORAGE_ERROR = "storage_error"


class ReviewPromptStatus(StrEnum):
    PENDING = "pending"
    ALREADY_COMPLETED = "already_completed"
    EMPTY = "empty"
    STORAGE_ERROR = "storage_error"


class ReviewCompletionStatus(StrEnum):
    COMPLETED = "completed"
    INVALID = "invalid"
    NO_PENDING = "no_pending"
    STORAGE_ERROR = "storage_error"


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    id: int
    entry_id: int
    review_date: date
    status: str
    prompted_at: datetime
    answered_at: datetime | None
    answer_text: str | None


@dataclass(frozen=True, slots=True)
class ReviewPromptResult:
    status: ReviewPromptStatus
    event: ReviewEvent | None = None
    entry: VocabularyEntry | None = None


@dataclass(frozen=True, slots=True)
class ReviewCompletionResult:
    status: ReviewCompletionStatus
    entry: VocabularyEntry | None = None
    answer_text: str | None = None
