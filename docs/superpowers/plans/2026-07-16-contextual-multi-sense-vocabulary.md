# Contextual Multi-Sense Vocabulary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve frictionless one-word capture while allowing optional Telegram context and multiple distinct senses under one vocabulary word.

**Architecture:** Split word identity/review state from child sense cards in SQLite. A pure parser recognizes one-line and multiline capture requests; Hermes performs semantic sense classification, while deterministic Python validates state-dependent operations and owns transactions. Daily scheduling remains word-level and reveals all stored senses after completion.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, dataclasses and `StrEnum`, Hermes Agent plugin API, SQLite migrations, pytest.

**Approved specification:** `docs/superpowers/specs/2026-07-16-contextual-multi-sense-vocabulary-design.md`

**Repository note:** This workspace currently has no `.git` directory. Commit steps below are required checkpoints after the project is placed under Git; do not initialize or publish a repository implicitly during execution.

---

## File Structure

### Create

- `src/hermes_vocab/migrations/002_multi_sense.sql` — transactional conversion from entry cards to words plus senses.
- `tests/unit/test_capture_parser.py` — message-shape contracts independent of Hermes.

### Modify

- `src/hermes_vocab/models.py` — word, sense, request, operation, and aggregate result types.
- `src/hermes_vocab/database.py` — ordered migration application through schema version 2.
- `src/hermes_vocab/capture.py` — message parsing, aggregate lookup, and state-validated capture operations.
- `src/hermes_vocab/review.py` — word-level selection and all-sense reveal data.
- `src/hermes_vocab/formatting.py` — deterministic new-sense, repeated-sense, and multi-sense text.
- `src/hermes_vocab/hermes_plugin/__init__.py` — inject capture lookup into the hook.
- `src/hermes_vocab/hermes_plugin/hooks.py` — contextual capture routing and existing-sense context.
- `src/hermes_vocab/hermes_plugin/schemas.py` — state-aware save tool schema.
- `src/hermes_vocab/hermes_plugin/tools.py` — map tool arguments to validated domain commands.
- `src/hermes_vocab/hermes_plugin/skills/vocabulary/SKILL.md` — exact semantic classification protocol.
- `tests/integration/test_database.py` — fresh-v2 and v1-to-v2 migration proof.
- `tests/unit/test_capture.py` — new-word, new-sense, existing-sense, conflict, and concurrency contracts.
- `tests/unit/test_formatting.py` — deterministic capture formats.
- `tests/unit/test_review.py` — word-level review state with sense aggregates.
- `tests/unit/test_daily_review.py` — unchanged question plus multi-sense completion output.
- `tests/integration/test_hermes_plugin.py` — real registration shape and contextual tool flow.
- `README.md` — contextual capture syntax and migration behavior.

No changes are required in `scripts/daily_review.py`; its service boundary remains stable.

---

### Task 1: Add Capture Request Parsing and Multi-Sense Domain Types

**Files:**
- Create: `tests/unit/test_capture_parser.py`
- Modify: `src/hermes_vocab/models.py:8-78`
- Modify: `src/hermes_vocab/capture.py:11-31`

- [ ] **Step 1: Write failing parser tests**

Create `tests/unit/test_capture_parser.py` with concrete shape contracts:

```python
from hermes_vocab.capture import parse_capture_message
from hermes_vocab.models import CaptureRequest


def test_one_word_produces_context_free_request() -> None:
    assert parse_capture_message("  obdurate  ") == CaptureRequest("obdurate", None)


def test_first_line_word_and_remaining_text_produce_context() -> None:
    assert parse_capture_message(
        "\nbank\nShe sat on the bank.\nThe river was high.\n"
    ) == CaptureRequest(
        "bank",
        "She sat on the bank.\nThe river was high.",
    )


def test_non_lexical_first_line_is_not_capture() -> None:
    assert parse_capture_message("How are you?\nI am reading.") is None


def test_command_is_not_capture() -> None:
    assert parse_capture_message("/help\nword") is None


def test_internal_blank_context_lines_are_preserved() -> None:
    assert parse_capture_message("bank\nfirst\n\nsecond") == CaptureRequest(
        "bank", "first\n\nsecond"
    )
```

- [ ] **Step 2: Run the parser tests and confirm red**

Run:

```bash
uv run --extra dev pytest tests/unit/test_capture_parser.py -q
```

Expected: collection fails because `CaptureRequest` and `parse_capture_message` do not exist.

- [ ] **Step 3: Add the domain types without removing old call-site types yet**

Add these exact contracts to `models.py`; old `EntryCard` and `VocabularyEntry` remain only until Tasks 3–4 migrate every caller:

```python
class CaptureOperation(StrEnum):
    NEW_WORD = "new_word"
    NEW_SENSE = "new_sense"
    EXISTING_SENSE = "existing_sense"


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
```

Extend `CaptureStatus` with `NEW_SENSE_SAVED` and `CONFLICT`. Keep the existing `CaptureResult` shape temporarily so the untouched capture service remains importable; Task 3 performs the clean result-model cutover after its failing tests establish every caller.

- [ ] **Step 4: Implement the pure parser**

Add to `capture.py` beside `is_lexical_word`:

```python
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
```

This intentionally treats any Telegram message whose first non-empty line is one lexical word as capture, matching the approved prefix-free syntax.

- [ ] **Step 5: Run parser tests and existing lexical tests**

Run:

```bash
uv run --extra dev pytest tests/unit/test_capture_parser.py tests/unit/test_capture.py::test_unicode_letters_and_internal_joiners_are_valid -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit checkpoint when Git exists**

```bash
git add src/hermes_vocab/models.py src/hermes_vocab/capture.py tests/unit/test_capture_parser.py
git commit -m "Model contextual vocabulary senses

Constraint: Preserve prefix-free Telegram capture and existing lexical rules
Confidence: high
Scope-risk: narrow
Tested: parser and Unicode lexical tests"
```

---

### Task 2: Migrate SQLite from Entries to Words and Senses

**Files:**
- Create: `src/hermes_vocab/migrations/002_multi_sense.sql`
- Modify: `src/hermes_vocab/database.py:20-33`
- Modify: `tests/integration/test_database.py:13-60`

- [ ] **Step 1: Write failing fresh-schema and migration tests**

Update the fresh-schema assertion to require `vocabulary_words`, `vocabulary_senses`, and `review_events`, with `PRAGMA user_version == 2`.

Add a v1 fixture by applying `001_initial.sql` directly, inserting one entry and one answered review event, then call `Database(path).initialize()`. Assert:

```python
assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
assert dict(connection.execute(
    "SELECT id, word, normalized_word, date_added, last_reviewed, review_status "
    "FROM vocabulary_words"
).fetchone()) == {
    "id": 7,
    "word": "bank",
    "normalized_word": "bank",
    "date_added": "2026-01-01T00:00:00Z",
    "last_reviewed": "2026-02-01T00:00:00Z",
    "review_status": "reviewed",
}
assert dict(connection.execute(
    "SELECT word_id, definition, part_of_speech, example_sentence, "
    "source_context, date_added FROM vocabulary_senses"
).fetchone()) == {
    "word_id": 7,
    "definition": "A financial institution.",
    "part_of_speech": "noun",
    "example_sentence": "She visited the bank.",
    "source_context": None,
    "date_added": "2026-01-01T00:00:00Z",
}
assert connection.execute(
    "SELECT word_id, status, answer_text FROM review_events"
).fetchone() == (7, "answered", "financial institution")
assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
```

Also reopen the migrated database and assert counts remain unchanged.

Extract the v1 fixture setup into `create_v1_database(path)`, then add an injected-failure rollback test:

```python
def test_failed_migration_rolls_back_to_intact_v1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "vocabulary.sqlite3"
    create_v1_database(path)
    original = Database._migration_sql
    broken_v2 = """
    BEGIN IMMEDIATE;
    CREATE TABLE partial_v2 (id INTEGER PRIMARY KEY);
    INSERT INTO missing_table VALUES (1);
    """

    monkeypatch.setattr(
        Database,
        "_migration_sql",
        staticmethod(lambda target: broken_v2 if target == 2 else original(target)),
    )

    with pytest.raises(sqlite3.OperationalError):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_entries"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'partial_v2'"
        ).fetchone()[0] == 0
```

- [ ] **Step 2: Run migration tests and confirm red**

```bash
uv run --extra dev pytest tests/integration/test_database.py -q
```

Expected: failures because initialization stops at version 1 and v2 tables do not exist.

- [ ] **Step 3: Create transactional migration 002**

Write `002_multi_sense.sql` with this structure:

```sql
BEGIN IMMEDIATE;

ALTER TABLE vocabulary_entries RENAME TO vocabulary_entries_v1;
ALTER TABLE review_events RENAME TO review_events_v1;

CREATE TABLE vocabulary_words (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL,
    normalized_word TEXT NOT NULL UNIQUE,
    date_added TEXT NOT NULL,
    last_reviewed TEXT,
    review_status TEXT NOT NULL DEFAULT 'new'
        CHECK (review_status IN ('new', 'reviewed'))
);

CREATE TABLE vocabulary_senses (
    id INTEGER PRIMARY KEY,
    word_id INTEGER NOT NULL REFERENCES vocabulary_words(id) ON DELETE CASCADE,
    definition TEXT NOT NULL,
    part_of_speech TEXT NOT NULL,
    example_sentence TEXT NOT NULL,
    source_context TEXT,
    date_added TEXT NOT NULL
);

CREATE TABLE review_events (
    id INTEGER PRIMARY KEY,
    word_id INTEGER NOT NULL REFERENCES vocabulary_words(id),
    review_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'missed')),
    prompted_at TEXT NOT NULL,
    answered_at TEXT,
    answer_text TEXT
);

INSERT INTO vocabulary_words (
    id, word, normalized_word, date_added, last_reviewed, review_status
)
SELECT id, word, normalized_word, date_added, last_reviewed, review_status
FROM vocabulary_entries_v1;

INSERT INTO vocabulary_senses (
    word_id, definition, part_of_speech, example_sentence, source_context, date_added
)
SELECT id, definition, part_of_speech, example_sentence, NULL, date_added
FROM vocabulary_entries_v1;

INSERT INTO review_events (
    id, word_id, review_date, status, prompted_at, answered_at, answer_text
)
SELECT id, entry_id, review_date, status, prompted_at, answered_at, answer_text
FROM review_events_v1;

DROP TABLE review_events_v1;
DROP TABLE vocabulary_entries_v1;

CREATE INDEX vocabulary_senses_word_order_idx
    ON vocabulary_senses(word_id, date_added, id);
CREATE INDEX review_events_word_id_idx ON review_events(word_id);
CREATE INDEX vocabulary_review_order_idx
    ON vocabulary_words(last_reviewed, date_added, id);

PRAGMA user_version = 2;
```

Do not put `COMMIT` in the resource. The Python runner must verify foreign keys before committing.

- [ ] **Step 4: Generalize the migration runner**

Replace the version switch in `Database.initialize()` with ordered resources:

```python
_MIGRATIONS = {
    1: "001_initial.sql",
    2: "002_multi_sense.sql",
}


@staticmethod
def _migration_sql(target: int) -> str:
    return files("hermes_vocab.migrations").joinpath(
        _MIGRATIONS[target]
    ).read_text(encoding="utf-8")


def _apply_migration(
    self,
    connection: sqlite3.Connection,
    target: int,
) -> None:
    connection.executescript(self._migration_sql(target))
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(f"Foreign-key violations: {violations}")
    connection.commit()
```

`initialize()` loops from `current + 1` through `max(_MIGRATIONS)`, rejects versions above 2, and rolls back any migration exception through the existing connection context manager. The `_migration_sql` seam exists only to make rollback behavior fault-injectable without changing packaged resources.

- [ ] **Step 5: Run database tests**

```bash
uv run --extra dev pytest tests/integration/test_database.py -q
```

Expected: all database tests pass with schema version 2 and preserved v1 data.

- [ ] **Step 6: Continue directly into Task 3**

Do not commit here. Applying schema v2 intentionally makes the still-v1 capture and review SQL fail. Tasks 2–4 are one atomic red-green cutover; the first committable state is after Task 4's full core suite passes.

---

### Task 3: Implement State-Validated Word and Sense Capture

**Files:**
- Modify: `src/hermes_vocab/models.py:8-40`
- Modify: `src/hermes_vocab/capture.py:34-117`
- Modify: `src/hermes_vocab/formatting.py:14-36`
- Modify: `tests/unit/test_capture.py`
- Modify: `tests/unit/test_formatting.py`

- [ ] **Step 1: Replace old capture tests with operation contracts**

Use these helpers and operation-level assertions:

```python
def command(
    operation: CaptureOperation,
    *,
    word: str = "bank",
    definition: str = "A financial institution.",
    context: str | None = None,
    matching_sense_id: int | None = None,
) -> CaptureCommand:
    card = (
        None
        if operation is CaptureOperation.EXISTING_SENSE
        else SenseCard(
            part_of_speech="noun",
            definition=definition,
            example_sentence="She visited the bank.",
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
    result = service.capture(command(CaptureOperation.NEW_WORD))
    assert result.status is CaptureStatus.SAVED
    assert result.word is not None
    assert len(result.word.senses) == 1
    assert result.sense == result.word.senses[0]


def test_new_sense_preserves_word_review_fields(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    original = service.capture(command(CaptureOperation.NEW_WORD)).word
    result = service.capture(command(
        CaptureOperation.NEW_SENSE,
        definition="Land alongside a river.",
        context="She sat on the bank and watched the river.",
    ))
    assert result.status is CaptureStatus.NEW_SENSE_SAVED
    assert result.word is not None
    assert len(result.word.senses) == 2
    assert result.word.last_reviewed == original.last_reviewed
    assert result.word.review_status == original.review_status


def test_existing_sense_returns_match_without_write(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    first = service.capture(command(CaptureOperation.NEW_WORD))
    result = service.capture(command(
        CaptureOperation.EXISTING_SENSE,
        matching_sense_id=first.sense.id,
    ))
    assert result.status is CaptureStatus.ALREADY_EXISTS
    assert result.sense == first.sense
    assert len(result.word.senses) == 1


def test_state_mismatches_return_conflict(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    service.capture(command(CaptureOperation.NEW_WORD))
    duplicate_new = service.capture(command(CaptureOperation.NEW_WORD))
    missing_word = service.capture(command(
        CaptureOperation.NEW_SENSE,
        word="shore",
        definition="Land at the edge of water.",
    ))
    assert duplicate_new.status is CaptureStatus.CONFLICT
    assert duplicate_new.word is not None
    assert missing_word.status is CaptureStatus.CONFLICT
    assert missing_word.word is None


def test_existing_sense_rejects_id_from_another_word(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    service.capture(command(CaptureOperation.NEW_WORD))
    shore = service.capture(command(
        CaptureOperation.NEW_WORD,
        word="shore",
        definition="Land at the edge of water.",
    ))
    result = service.capture(command(
        CaptureOperation.EXISTING_SENSE,
        matching_sense_id=shore.sense.id,
    ))
    assert result.status is CaptureStatus.CONFLICT
    assert len(result.word.senses) == 1


def test_oversized_context_does_not_write(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    result = service.capture(command(
        CaptureOperation.NEW_WORD,
        context="x" * 2001,
    ))
    assert result.status is CaptureStatus.INVALID
    assert service.get_word("bank") is None


def test_concurrent_new_word_creates_one_word(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _: service.capture(command(CaptureOperation.NEW_WORD)),
            range(2),
        ))
    assert {result.status for result in results} == {
        CaptureStatus.SAVED,
        CaptureStatus.CONFLICT,
    }
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_words"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_senses"
        ).fetchone()[0] == 1
```

Add formatter assertions:

```python
assert format_capture(new_word_result).endswith("✓ Saved.")
assert format_capture(new_sense_result).endswith("✓ New meaning saved.")
assert format_capture(existing_result).endswith("Already saved with this meaning.")
assert "Saved" not in format_capture(conflict_result)
```

- [ ] **Step 2: Run capture and formatting tests and confirm red**

```bash
uv run --extra dev pytest tests/unit/test_capture.py tests/unit/test_formatting.py -q
```

Expected: failures because capture still writes `vocabulary_entries` and lacks operation validation.

- [ ] **Step 3: Finalize capture models and remove obsolete entry types**

Make `CaptureStatus` exactly:

```python
class CaptureStatus(StrEnum):
    SAVED = "saved"
    NEW_SENSE_SAVED = "new_sense_saved"
    ALREADY_EXISTS = "already_exists"
    INVALID = "invalid"
    CONFLICT = "conflict"
    STORAGE_ERROR = "storage_error"
```

Replace the old result with:

```python
@dataclass(frozen=True, slots=True)
class CaptureResult:
    status: CaptureStatus
    word: VocabularyWord | None = None
    sense: VocabularySense | None = None
```

Keep `EntryCard`, `VocabularyEntry`, and the legacy `_entry_from_row` helper temporarily because the untouched review service still imports them. No capture or formatting path may use them after this task; Task 4 removes all three once review callers are converted.

- [ ] **Step 4: Add aggregate row mapping and lookup**

Implement private `_word_from_rows(word_row, sense_rows)` and public:

```python
def get_word(self, word: str) -> VocabularyWord | None:
    normalized = normalize_word(word)
    with self.database.connect() as connection:
        word_row = connection.execute(
            "SELECT * FROM vocabulary_words WHERE normalized_word = ?",
            (normalized,),
        ).fetchone()
        if word_row is None:
            return None
        sense_rows = connection.execute(
            "SELECT * FROM vocabulary_senses WHERE word_id = ? "
            "ORDER BY date_added, id",
            (word_row["id"],),
        ).fetchall()
    return _word_from_rows(word_row, sense_rows)
```

- [ ] **Step 5: Implement transactional operation validation**

`CaptureService.capture(command)` must:

1. validate lexical word, context length (`<= 2000`), and card bounds;
2. start `BEGIN IMMEDIATE`;
3. load the current word by normalized key;
4. enforce:
   - `NEW_WORD`: current word absent, complete card present, no matching ID;
   - `NEW_SENSE`: current word present, complete card present, no matching ID;
   - `EXISTING_SENSE`: current word present, no new card, matching ID belongs to it;
5. insert only for the two write operations;
6. reload the aggregate inside the transaction;
7. commit and return the created or matched sense.

Return `CONFLICT` for state mismatches and include the current word aggregate in the result when one exists; return `INVALID` for malformed commands. Catch `sqlite3.Error` as `STORAGE_ERROR`. Do not retry inside the domain service. On conflict, the plugin returns this refreshed state to the model for at most one retry.

- [ ] **Step 6: Format capture outcomes deterministically**

Change `_card` to accept `VocabularyWord` plus `VocabularySense`. Map statuses:

```python
INVALID -> "Send one word, optionally followed by context on the next line."
CONFLICT -> "That word changed while I was saving it. Please try again."
STORAGE_ERROR -> existing storage error
ALREADY_EXISTS -> card + "Already saved with this meaning."
NEW_SENSE_SAVED -> card + "✓ New meaning saved."
SAVED -> card + "✓ Saved."
```

- [ ] **Step 7: Run capture, formatter, and database tests**

```bash
uv run --extra dev pytest tests/unit/test_capture.py tests/unit/test_formatting.py tests/integration/test_database.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Continue directly into Task 4**

Do not commit here. Capture now uses schema v2, but review still uses the v1 tables. Complete the review cutover before running the full suite or recording a checkpoint.

---

### Task 4: Keep Reviews Word-Level and Reveal All Senses

**Files:**
- Modify: `src/hermes_vocab/models.py:42-78`
- Modify: `src/hermes_vocab/review.py:1-180`
- Modify: `src/hermes_vocab/formatting.py:39-59`
- Modify: `tests/unit/test_review.py`
- Modify: `tests/unit/test_daily_review.py`

- [ ] **Step 1: Rewrite review fixtures around word aggregates**

Create the two-sense fixture through the public capture service, then assert the word-level review aggregate:

```python
capture = CaptureService(database, clock=lambda: NOW)
capture.capture(CaptureCommand(
    word="bank",
    operation=CaptureOperation.NEW_WORD,
    card=SenseCard(
        part_of_speech="noun",
        definition="A financial institution.",
        example_sentence="She deposited the cheque at the bank.",
    ),
))
capture.capture(CaptureCommand(
    word="bank",
    operation=CaptureOperation.NEW_SENSE,
    card=SenseCard(
        part_of_speech="noun",
        definition="Land alongside a river.",
        example_sentence="They rested on the grassy bank.",
    ),
    source_context="They rested on the bank beside the river.",
))

prompt = service.daily_review()
assert prompt.word is not None
assert len(prompt.word.senses) == 2
assert format_daily_review(prompt) == "What does 'bank' mean?"

completion = service.complete_review("financial institution or river edge")
assert completion.status is ReviewCompletionStatus.COMPLETED
assert completion.word is not None
assert len(completion.word.senses) == 2
```

Retain tests for empty library, blank answer, same-day retry, prior-day missed state, restart persistence, and concurrent same-day creation.

Add exact multi-sense output:

```text
1. noun — A financial institution.
   Example: She deposited the cheque at the bank.

2. noun — Land alongside a river.
   Example: They rested on the grassy bank.
```

Also prove a one-sense word still produces the existing `Definition:` / `Example:` output.

- [ ] **Step 2: Run review tests and confirm red**

```bash
uv run --extra dev pytest tests/unit/test_review.py tests/unit/test_daily_review.py -q
```

Expected: SQL errors referencing `vocabulary_entries`/`entry_id` or model mismatches.

- [ ] **Step 3: Rename event and result contracts**

Change `ReviewEvent.entry_id` to `word_id`. Change `ReviewPromptResult.entry` and `ReviewCompletionResult.entry` to `word: VocabularyWord | None`. Preserve status enums and `answer_text`. After `review.py` and its formatters compile against `VocabularyWord`, delete the now-unreferenced `EntryCard`, `VocabularyEntry`, and `_entry_from_row`; no aliases or compatibility exports remain.

- [ ] **Step 4: Convert review SQL and aggregate loading**

In `daily_review()`:

- select from `vocabulary_words` using the existing `last_reviewed, date_added, id` ordering;
- write `review_events.word_id`;
- load every sense ordered by `date_added, id`;
- return one word aggregate.

In `complete_review()`:

- join/load by `word_id`;
- update `vocabulary_words.last_reviewed` and `review_status` once;
- return every sense without modifying them.

Keep the same short transactions and local-date idempotency.

- [ ] **Step 5: Add one-sense and multi-sense completion formatting**

For one sense, retain:

```text
Definition:
<definition>

Example:
<example>
```

For multiple senses, join numbered blocks:

```python
return "\n\n".join(
    f"{index}. {sense.part_of_speech} — {sense.definition}\n"
    f"   Example: {sense.example_sentence}"
    for index, sense in enumerate(result.word.senses, start=1)
)
```

- [ ] **Step 6: Run the complete core cutover suite**

```bash
uv run --extra dev pytest \
  tests/integration/test_database.py \
  tests/unit/test_capture_parser.py \
  tests/unit/test_capture.py \
  tests/unit/test_formatting.py \
  tests/unit/test_review.py \
  tests/unit/test_daily_review.py -q
```

Expected: all selected tests pass, including migration rollback, capture concurrency, review lifecycle, and exact stdout behavior.

- [ ] **Step 7: Commit the atomic schema/domain cutover when Git exists**

```bash
git add \
  src/hermes_vocab/database.py \
  src/hermes_vocab/migrations/002_multi_sense.sql \
  src/hermes_vocab/models.py \
  src/hermes_vocab/capture.py \
  src/hermes_vocab/review.py \
  src/hermes_vocab/formatting.py \
  tests/integration/test_database.py \
  tests/unit/test_capture.py \
  tests/unit/test_formatting.py \
  tests/unit/test_review.py \
  tests/unit/test_daily_review.py
git commit -m "Preserve distinct meanings without expanding daily review

Constraint: Schema, capture, and review must cut over atomically
Rejected: JSON definitions | weak querying and migration semantics
Rejected: Sense-level scheduling | introduces spaced repetition complexity
Confidence: high
Scope-risk: moderate
Tested: migration rollback, capture operations, review lifecycle, exact outputs"
```

---

### Task 5: Teach the Hermes Plugin Contextual Sense Classification

**Files:**
- Modify: `src/hermes_vocab/hermes_plugin/__init__.py:15-38`
- Modify: `src/hermes_vocab/hermes_plugin/hooks.py:7-37`
- Modify: `src/hermes_vocab/hermes_plugin/schemas.py:4-25`
- Modify: `src/hermes_vocab/hermes_plugin/tools.py:5-42`
- Modify: `src/hermes_vocab/hermes_plugin/skills/vocabulary/SKILL.md`
- Modify: `tests/integration/test_hermes_plugin.py`

- [ ] **Step 1: Add failing plugin routing and tool tests**

Extend the fake-context integration tests with these helpers and assertions:

```python
def save_args(
    operation: str,
    *,
    word: str = "bank",
    definition: str = "A financial institution.",
    context: str | None = None,
    matching_sense_id: int | None = None,
) -> dict:
    args = {"word": word, "operation": operation}
    if operation != "existing_sense":
        args.update({
            "part_of_speech": "noun",
            "definition": definition,
            "example_sentence": "She visited the bank.",
        })
    if context is not None:
        args["source_context"] = context
    if matching_sense_id is not None:
        args["matching_sense_id"] = matching_sense_id
    return args


def test_contextual_capture_injects_verbatim_context(monkeypatch, tmp_path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    guidance = hook_call(
        context.hooks["pre_llm_call"],
        "bank\\nShe sat on the bank and watched the river.",
    )
    assert '\"context\": \"She sat on the bank and watched the river.\"' in guidance
    assert '\"senses\": []' in guidance


def test_existing_word_guidance_contains_numbered_sense_id(monkeypatch, tmp_path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    saved = json.loads(context.tools["vocabulary_save_card"](
        save_args("new_word")
    ))
    guidance = hook_call(context.hooks["pre_llm_call"], "bank")
    assert saved["status"] == "saved"
    assert '\"id\": 1' in guidance
    assert '\"definition\": \"A financial institution.\"' in guidance


def test_new_sense_tool_appends_context(monkeypatch, tmp_path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    tool = context.tools["vocabulary_save_card"]
    tool(save_args("new_word"))
    result = json.loads(tool(save_args(
        "new_sense",
        definition="Land alongside a river.",
        context="She sat on the bank and watched the river.",
    )))
    assert result["status"] == "new_sense_saved"
    assert result["text"].endswith("✓ New meaning saved.")
    with Database(path).connect() as connection:
        row = connection.execute(
            "SELECT source_context FROM vocabulary_senses ORDER BY id DESC"
        ).fetchone()
    assert row[0] == "She sat on the bank and watched the river."


def test_existing_sense_tool_performs_no_write(monkeypatch, tmp_path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    tool = context.tools["vocabulary_save_card"]
    tool(save_args("new_word"))
    result = json.loads(tool(save_args(
        "existing_sense",
        matching_sense_id=1,
    )))
    assert result["status"] == "already_exists"
    assert result["text"].endswith("Already saved with this meaning.")
    with Database(path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM vocabulary_senses"
        ).fetchone()[0] == 1


def test_cross_word_sense_id_returns_refreshed_conflict(monkeypatch, tmp_path) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    tool = context.tools["vocabulary_save_card"]
    tool(save_args("new_word"))
    tool(save_args(
        "new_word",
        word="shore",
        definition="Land at the edge of water.",
    ))
    result = json.loads(tool(save_args(
        "existing_sense",
        matching_sense_id=2,
    )))
    assert result["status"] == "conflict"
    assert result["state"]["word_exists"] is True
    assert [sense["id"] for sense in result["state"]["senses"]] == [1]


def test_pending_review_precedes_contextual_capture(monkeypatch, tmp_path) -> None:
    context, path = register_plugin(monkeypatch, tmp_path)
    context.tools["vocabulary_save_card"](save_args("new_word"))
    settings = Settings.from_environment()
    ReviewService(
        Database(path),
        settings.timezone,
        clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC),
    ).daily_review()
    guidance = hook_call(
        context.hooks["pre_llm_call"],
        "shore\\nThey walked along the shore.",
    )
    assert "vocabulary_complete_review" in guidance
    assert "vocabulary_save_card" not in guidance


def test_non_telegram_multiline_message_is_not_auto_capture(
    monkeypatch, tmp_path
) -> None:
    context, _ = register_plugin(monkeypatch, tmp_path)
    assert hook_call(
        context.hooks["pre_llm_call"],
        "bank\\nShe sat beside the river.",
        platform="cli",
    ) is None
```

Import `Settings` and `ReviewService` at module scope with the existing `Database`, `UTC`, and `datetime` imports. Assert returned JSON statuses and exact formatter text rather than SQL text fragments.

- [ ] **Step 2: Run plugin tests and confirm red**

```bash
uv run --extra dev pytest tests/integration/test_hermes_plugin.py -q
```

Expected: failures because the hook only recognizes a single lexical message and the tool accepts only a flat card.

- [ ] **Step 3: Extend the save schema**

Define `SAVE_CARD.parameters.properties` with:

```python
{
    "word": {"type": "string"},
    "operation": {
        "type": "string",
        "enum": ["new_word", "new_sense", "existing_sense"],
    },
    "source_context": {"type": "string"},
    "matching_sense_id": {"type": "integer"},
    "part_of_speech": {"type": "string"},
    "definition": {"type": "string"},
    "example_sentence": {"type": "string"},
}
```

Require only `word` and `operation` at JSON-schema level. Domain validation enforces operation-specific fields. Update the description to state that source context must be copied verbatim and an existing sense must use its supplied ID.

- [ ] **Step 4: Convert tool arguments to a `CaptureCommand`**

In `save_card`, parse `CaptureOperation`; build a `SenseCard` only when all three card fields are strings; copy `source_context` without model-side rewriting; and pass `matching_sense_id` through. Invalid enum/type input returns `CaptureStatus.INVALID` without a database write.

Return `status` and `text` for every result. On `CONFLICT`, also return authoritative refreshed state so the model can retry once without a third tool:

```python
{
    "status": result.status.value,
    "text": format_capture(result),
    "state": {
        "word_exists": result.word is not None,
        "senses": [
            {
                "id": sense.id,
                "part_of_speech": sense.part_of_speech,
                "definition": sense.definition,
            }
            for sense in result.word.senses
        ] if result.word else [],
    },
}
```

Omit `state` for non-conflict outcomes.

- [ ] **Step 5: Inject contextual and existing-sense guidance from the hook**

Change `VocabularyHook` to receive both `CaptureService` and `ReviewService`. Keep pending review as the first branch. Then:

1. call `parse_capture_message(user_message)`;
2. return `None` outside Telegram or for no request;
3. load `capture_service.get_word(request.word)`;
4. serialize only sense ID, part of speech, and definition into the ephemeral guidance;
5. include source context verbatim when present;
6. require exactly one operation and one `vocabulary_save_card` call;
7. instruct the model to relay tool text verbatim.

Use `json.dumps(..., ensure_ascii=False)` for embedded data so quotes/newlines cannot alter the instruction structure.

- [ ] **Step 6: Update registration and skill instructions**

Construct the hook as:

```python
hook = VocabularyHook(capture_service, review_service)
```

Update `SKILL.md` to define:

- `new_word` only when no stored word is supplied;
- `new_sense` only for a genuinely distinct meaning;
- `existing_sense` for paraphrases of a stored meaning, with its exact ID;
- context is evidence, not text to copy into the generated example;
- no follow-up question and no success claim outside tool output;
- on `conflict`, use returned `state` for at most one corrected tool call, then relay the conflict text if state changes again;
- pending review behavior remains unchanged.

- [ ] **Step 7: Run plugin and core tests**

```bash
uv run --extra dev pytest tests/integration/test_hermes_plugin.py tests/unit/test_capture_parser.py tests/unit/test_capture.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Smoke-test real Hermes discovery**

Run with the configured local environment:

```bash
HERMES_PLUGINS_DEBUG=1 hermes prompt-size
hermes tools list --platform telegram
```

Expected plugin log:

```text
Plugin vocabulary registered tool: vocabulary_save_card
Plugin vocabulary registered tool: vocabulary_complete_review
Plugin vocabulary registered hook: pre_llm_call
Plugin vocabulary registered skill: vocabulary:vocabulary
```

Expected Telegram toolset: `vocabulary` enabled.

- [ ] **Step 9: Commit checkpoint when Git exists**

```bash
git add src/hermes_vocab/hermes_plugin tests/integration/test_hermes_plugin.py
git commit -m "Use reading context to preserve distinct word senses

Constraint: Capture remains one Telegram message with no follow-up
Directive: Never trust model-selected sense IDs without word ownership checks
Confidence: high
Scope-risk: moderate
Tested: plugin routing, tool states, pending-review priority, real discovery"
```

---

### Task 6: Document, Package, and Verify the End-to-End Cutover

**Files:**
- Modify: `README.md`
- Verify: `pyproject.toml`
- Verify: `scripts/daily_review.py`
- Verify: all tests

- [ ] **Step 1: Update user-facing capture documentation**

Add the accepted contextual syntax and outcomes to `README.md`:

```text
bank
She sat on the bank and watched the river.
```

Document that:

- one-word capture is unchanged;
- repeating the same meaning is idempotent;
- a distinct meaning adds a child sense;
- any Telegram message beginning with a standalone lexical first line uses contextual capture because there is no explicit prefix;
- review still asks one word and reveals all stored senses;
- existing databases migrate automatically on first initialization;
- a backup should be taken before upgrade and the gateway should be stopped during backup/restore.

- [ ] **Step 2: Run the full behavioral suite**

```bash
uv run --extra dev pytest
```

Expected: all tests pass with no skipped feature-contract tests beyond existing platform-specific permission skips.

- [ ] **Step 3: Build and inspect package data**

```bash
uv build
python3 -m zipfile -l dist/hermes_vocab-0.1.0-py3-none-any.whl
```

Expected wheel contents include:

```text
hermes_vocab/migrations/001_initial.sql
hermes_vocab/migrations/002_multi_sense.sql
hermes_vocab/hermes_plugin/skills/vocabulary/SKILL.md
```

- [ ] **Step 4: Exercise a real temporary-database smoke scenario**

Run the existing integration surface or a short checked-in test—not an ad-hoc production database mutation—to prove this sequence:

1. initialize schema v2;
2. save `bank` as a financial institution;
3. append river-edge sense with source context;
4. classify the financial meaning as existing and confirm no third row;
5. create the morning review;
6. complete it and observe two numbered senses;
7. rerun the same-day review and observe empty output;
8. reopen the database and confirm one word, two senses, one answered review event.

The proof should be implemented as `test_multi_sense_capture_review_survives_restart` in `tests/integration/test_hermes_plugin.py`, then run:

```bash
uv run --extra dev pytest tests/integration/test_hermes_plugin.py::test_multi_sense_capture_review_survives_restart -q
```

Expected: one passing test.

- [ ] **Step 5: Reinstall the editable package and smoke installed script**

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python -e .
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/daily_review.py
```

Expected with the configured production database: either one exact review question, the empty-library guidance, or no output for an already-completed day. Any of those is valid based on current authoritative state; traceback or schema error is not.

- [ ] **Step 6: Verify scheduler and gateway state without sending externally**

```bash
hermes cron list
hermes gateway status
```

Expected: `daily-vocabulary-review` still uses `daily_review.py`, no-agent mode, Telegram delivery, and the existing schedule. Do not manually run the Telegram-delivery job unless credentials and the private home DM are configured.

- [ ] **Step 7: Commit checkpoint when Git exists**

```bash
git add README.md tests/integration/test_hermes_plugin.py
git commit -m "Explain contextual capture without adding review friction

Constraint: Existing setup and daily schedule remain operational
Confidence: high
Scope-risk: narrow
Tested: full pytest suite, wheel contents, restart smoke, installed script, Hermes discovery"
```

---

## Final Verification Matrix

| Claim | Proof |
|---|---|
| Existing cards and review history survive | v1-to-v2 integration migration test |
| One-word capture remains unchanged | parser, capture, and plugin tests |
| Multiline context selects a sense without follow-up | plugin routing/tool integration test |
| Distinct meanings coexist under one word | capture and restart smoke tests |
| Repeated meanings do not write | existing-sense row-count tests |
| Stale/cross-word model output cannot corrupt state | conflict and sense-ownership tests |
| Daily review remains one question | review and daily-script exact-output tests |
| All senses are revealed concisely | one-sense and multi-sense formatter tests |
| Package ships migration and skill data | wheel listing |
| Current Hermes loads the plugin | `HERMES_PLUGINS_DEBUG=1 hermes prompt-size` |

## Explicit Non-Goals

Do not add embeddings, fuzzy definition matching, an ORM, a dictionary API, sense-level review schedules, grading, correction conversations, Telegram reply metadata, or Hermes core patches. They are not needed to satisfy the approved specification.
