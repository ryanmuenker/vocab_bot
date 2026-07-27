from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scheduling import ScheduleTransition


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




class EvaluationGrade(StrEnum):
    CORRECT = "correct"
    PARTIAL = "partial"
    INCORRECT = "incorrect"


@dataclass(frozen=True, slots=True)
class Evaluation:
    grade: EvaluationGrade
    feedback: str


@dataclass(frozen=True, slots=True)
class TestSummary:
    correct: int = 0
    partial: int = 0
    incorrect: int = 0


class ReviewRating(StrEnum):
    AGAIN = "again"
    HARD = "hard"
    GOOD = "good"
    EASY = "easy"


class CardScheduleState(StrEnum):
    NEW = "new"
    REVIEW = "review"
    RELEARNING = "relearning"


class CardDirection(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"


class StudyMode(StrEnum):
    REVIEW = "review"
    TEST_FORWARD = "test_forward"
    TEST_REVERSE = "test_reverse"


class StudySessionStatus(StrEnum):
    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    EXITED = "exited"


class StudyQueueStatus(StrEnum):
    QUEUED = "queued"
    CURRENT = "current"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class StudyPromptStatus(StrEnum):
    PREPARED = "prepared"
    DELIVERED = "delivered"
    ANSWERED = "answered"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryAttemptStatus(StrEnum):
    UNKNOWN = "unknown"
    FAILED = "failed"
    DELIVERED = "delivered"


SCHEDULER_KIND = "fsrs-6"
SCHEDULER_VERSION = "fsrs-6.3.1-hermes-1"
PARAMETERS_VERSION = "py-fsrs-6.3.1-default"
PARAMETER_FINGERPRINT = (
    "sha256:a00444e09ca114a3ce9704158c2abb90200f9aa76e4892ef87fe7d4c79b85f56"
)


def _is_utc(value: datetime) -> bool:
    return (
        value.tzinfo is not None
        and value.utcoffset() == UTC.utcoffset(value)
        and value.tzname() == "UTC"
    )


@dataclass(frozen=True, slots=True)
class CardSchedule:
    state: CardScheduleState
    due: datetime
    stability: float | None = None
    difficulty: float | None = None
    last_review: datetime | None = None
    repetitions: int = 0
    lapses: int = 0
    scheduler_kind: str = SCHEDULER_KIND
    parameter_fingerprint: str = PARAMETER_FINGERPRINT
    desired_retention: float = 0.90
    scheduler_version: str = SCHEDULER_VERSION
    parameters_version: str = PARAMETERS_VERSION

    def __post_init__(self) -> None:
        if not _is_utc(self.due):
            raise ValueError("due must be a timezone-aware UTC datetime")
        if self.last_review is not None and not _is_utc(self.last_review):
            raise ValueError("last_review must be a timezone-aware UTC datetime")
        if self.repetitions < 0 or self.lapses < 0:
            raise ValueError("review counters cannot be negative")
        if self.state is CardScheduleState.NEW:
            if (
                self.stability is not None
                or self.difficulty is not None
                or self.last_review is not None
                or self.repetitions != 0
                or self.lapses != 0
            ):
                raise ValueError("new schedules cannot contain review state")
            return
        if self.lapses > self.repetitions:
            raise ValueError("review counters cannot have more lapses than repetitions")
        if self.stability is None or self.difficulty is None:
            raise ValueError("reviewed schedules require stability and difficulty")
        if self.last_review is None:
            raise ValueError("reviewed schedules require last_review")
        if self.repetitions < 1:
            raise ValueError("reviewed schedules require at least one repetition")
        if not isfinite(self.stability) or self.stability <= 0:
            raise ValueError("stability must be finite and positive")
        if not isfinite(self.difficulty) or not 1 <= self.difficulty <= 10:
            raise ValueError("difficulty must be finite and between 1 and 10")


class StudyStartStatus(StrEnum):
    STARTED = "started"
    RESUMED = "resumed"
    EMPTY = "empty"
    CONFLICT = "conflict"
    STORAGE_ERROR = "storage_error"


class StudyMutationStatus(StrEnum):
    COMPLETED = "completed"
    STALE = "stale"
    STORAGE_ERROR = "storage_error"


class FinalizeStatus(StrEnum):
    COMPLETED = "completed"
    STALE = "stale"
    NO_ANSWER = "no_answer"
    STORAGE_ERROR = "storage_error"


@dataclass(frozen=True, slots=True)
class StudyCardSnapshot:
    id: int
    entry_id: int
    sense_id: int | None
    direction: CardDirection
    state: CardScheduleState
    stability: float | None
    difficulty: float | None
    due: datetime
    effective_due: datetime
    last_review: datetime | None
    repetitions: int
    lapses: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StudyQueueItemSnapshot:
    id: int
    card: StudyCardSnapshot
    position: int
    status: StudyQueueStatus
    retry_of_queue_item_id: int | None


@dataclass(frozen=True, slots=True)
class StudyPromptSnapshot:
    id: int
    session_id: int
    queue_item_id: int
    prompt_key: str
    prompt_text: str
    status: StudyPromptStatus
    prepared_at: datetime
    delivered_at: datetime | None
    answered_at: datetime | None


@dataclass(frozen=True, slots=True)
class StudyProgress:
    completed: int
    total: int


@dataclass(frozen=True, slots=True)
class StudySnapshot:
    session_id: int
    mode: StudyMode
    status: StudySessionStatus
    local_date: date
    queue: tuple[StudyQueueItemSnapshot, ...]
    current_prompt: StudyPromptSnapshot | None
    progress: StudyProgress


@dataclass(frozen=True, slots=True)
class StudyStartResult:
    status: StudyStartStatus
    snapshot: StudySnapshot | None = None
    available_count: int | None = None


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    status: FinalizeStatus
    transition: ScheduleTransition | None = None
    snapshot: StudySnapshot | None = None


@dataclass(frozen=True, slots=True)
class StudyDraftSnapshot:
    id: int
    submitted_answer: str
    evaluation: Evaluation
    answered_at: datetime


@dataclass(frozen=True, slots=True)
class StudyAnswerContext:
    prompt: StudyPromptSnapshot
    queue_item: StudyQueueItemSnapshot
    entry: VocabularyEntry
    sense: VocabularySense | None
    draft: StudyDraftSnapshot | None = None


class StudyAnswerStatus(StrEnum):
    AWAITING_RATING = "awaiting_rating"
    FINALIZED = "finalized"
    INVALID_INPUT = "invalid_input"
    INVALID_RATING = "invalid_rating"
    EVALUATION_ERROR = "evaluation_error"
    STORAGE_ERROR = "storage_error"
    NO_ACTIVE = "no_active"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class StudyAnswerResult:
    status: StudyAnswerStatus
    context: StudyAnswerContext | None = None
    allowed_ratings: tuple[ReviewRating, ...] = ()
    finalization: FinalizeResult | None = None
