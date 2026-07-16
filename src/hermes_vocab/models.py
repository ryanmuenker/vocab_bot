from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class CaptureOperation(StrEnum):
    NEW_WORD = "new_word"
    NEW_SENSE = "new_sense"
    EXISTING_SENSE = "existing_sense"


class CaptureStatus(StrEnum):
    SAVED = "saved"
    NEW_SENSE_SAVED = "new_sense_saved"
    ALREADY_EXISTS = "already_exists"
    INVALID = "invalid"
    CONFLICT = "conflict"
    STORAGE_ERROR = "storage_error"


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    word: str
    context: str | None


@dataclass(frozen=True, slots=True)
class SenseCard:
    part_of_speech: str
    definition: str
    example_sentence: str


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    word: str
    operation: CaptureOperation
    card: SenseCard | None = None
    source_context: str | None = None
    matching_sense_id: int | None = None


@dataclass(frozen=True, slots=True)
class VocabularySense:
    id: int
    word_id: int
    definition: str
    part_of_speech: str
    example_sentence: str
    source_context: str | None
    date_added: datetime


@dataclass(frozen=True, slots=True)
class VocabularyWord:
    id: int
    word: str
    normalized_word: str
    date_added: datetime
    last_reviewed: datetime | None
    review_status: str
    senses: tuple[VocabularySense, ...]




@dataclass(frozen=True, slots=True)
class CaptureResult:
    status: CaptureStatus
    word: VocabularyWord | None = None
    sense: VocabularySense | None = None


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
    word_id: int
    review_date: date
    status: str
    prompted_at: datetime
    answered_at: datetime | None
    answer_text: str | None


@dataclass(frozen=True, slots=True)
class ReviewPromptResult:
    status: ReviewPromptStatus
    event: ReviewEvent | None = None
    word: VocabularyWord | None = None


@dataclass(frozen=True, slots=True)
class ReviewCompletionResult:
    status: ReviewCompletionStatus
    word: VocabularyWord | None = None
    answer_text: str | None = None
