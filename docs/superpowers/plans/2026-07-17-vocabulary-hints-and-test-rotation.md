# Vocabulary Hints and Test Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add state-preserving full-sentence hints to daily reviews and `/test`, and rotate test words by least-recent test use.

**Architecture:** Keep hint intent recognition at the deterministic Telegram gateway boundary, before semantic evaluation, and format the first stored example sentence through the existing formatting module. Reuse persisted `test_sessions` and `test_questions` history in the test-start selection query, with existing review priority as a stable tie-breaker; do not add schema or scheduling state.

**Tech Stack:** Python 3.11+, SQLite, pytest, Hermes Agent plugin gateway interception

**Design source:** `docs/superpowers/specs/2026-07-17-vocabulary-hints-and-test-rotation-design.md`

---

## File Structure

- Modify `src/hermes_vocab/formatting.py`: own the exact `Hint: ...` user-facing response.
- Modify `src/hermes_vocab/hermes_plugin/gateway.py`: normalize bounded hint phrases and route hints before review/test evaluation.
- Modify `src/hermes_vocab/test_session.py`: select entries by least-recent persisted test appearance, then existing review priority.
- Modify `tests/unit/test_formatting.py`: lock full-sentence and first-sense hint formatting.
- Modify `tests/unit/test_gateway_routing.py`: lock hint recognition, state preservation, evaluator bypass, precedence, and post-hint answer behavior.
- Modify `tests/unit/test_test_session.py`: lock unseen-first and least-recently-tested rotation without review mutation.
- Modify `README.md`: describe hint phrases, state preservation, and test rotation.

No migration, model, evaluator, tool schema, plugin command, or Hermes core file changes are required.

**Execution safety:** This checkout already contains uncommitted prerequisite graded-review and `/test` changes. The per-task commit commands below are valid only after those prerequisite changes have been committed separately or the user explicitly authorizes bundling them. When executing immediately in the current checkout, skip the commit steps and preserve the existing uncommitted work.

---

### Task 1: Add deterministic hint formatting

**Files:**
- Modify: `src/hermes_vocab/formatting.py:19-49`
- Test: `tests/unit/test_formatting.py:5-49`

- [ ] **Step 1: Write the failing formatter tests**

Add `format_hint` to the import list and add these tests after the `WORD` fixture:

```python
def test_hint_returns_complete_stored_example_with_word() -> None:
    assert format_hint(WORD) == (
        "Hint: The committee remained obdurate despite new evidence."
    )


def test_hint_uses_first_stored_sense_deterministically() -> None:
    second = VocabularySense(
        id=3,
        entry_id=WORD.id,
        definition="Resistant to persuasion.",
        part_of_speech="adjective",
        example_sentence="The witness remained obdurate.",
        source_context=None,
        date_added=WORD.date_added,
    )
    entry = VocabularyEntry(
        id=WORD.id,
        display_text=WORD.display_text,
        normalized_text=WORD.normalized_text,
        date_added=WORD.date_added,
        last_reviewed=WORD.last_reviewed,
        review_status=WORD.review_status,
        senses=(SENSE, second),
    )

    assert format_hint(entry) == (
        "Hint: The committee remained obdurate despite new evidence."
    )
```

- [ ] **Step 2: Run the formatter tests and confirm RED**

Run:

```bash
uv run --extra dev pytest tests/unit/test_formatting.py -q
```

Expected: collection fails because `format_hint` is not exported from `hermes_vocab.formatting`.

- [ ] **Step 3: Implement the minimal formatter**

Add this public formatter immediately after `format_entry`:

```python
def format_hint(entry: VocabularyEntry) -> str:
    return f"Hint: {entry.senses[0].example_sentence}"
```

The persistence contract guarantees at least one validated sense per reviewable/testable entry. Do not add generated fallback text or copy the definition.

- [ ] **Step 4: Run the formatter tests and confirm GREEN**

Run:

```bash
uv run --extra dev pytest tests/unit/test_formatting.py -q
```

Expected: all tests in `tests/unit/test_formatting.py` pass.

- [ ] **Step 5: Commit the formatter contract**

```bash
git add src/hermes_vocab/formatting.py tests/unit/test_formatting.py
git commit -m "Keep study hints grounded in saved examples" -m "Constraint: Hint output must include the stored word and must not invoke a model
Confidence: high
Scope-risk: narrow
Tested: uv run --extra dev pytest tests/unit/test_formatting.py -q"
```

---

### Task 2: Route hint requests without consuming study state

**Files:**
- Modify: `src/hermes_vocab/hermes_plugin/gateway.py:7-108`
- Test: `tests/unit/test_gateway_routing.py:194-543`

- [ ] **Step 1: Write failing daily-review hint tests**

Add a module-level phrase table near the gateway test fixtures:

```python
HINT_REQUESTS = [
    "hint",
    "HINT",
    "  give   me a hint  ",
    "Can I have a hint?",
    "show me an example.",
    "example sentence!",
]
```

Add this parameterized test after the existing pending-review grading test:

```python
@pytest.mark.parametrize("request", HINT_REQUESTS)
def test_daily_review_hint_preserves_pending_answer_state(
    tmp_path: Path,
    request: str,
) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (
            SenseCard("adjective", "Using few words.", "His reply was laconic."),
            SenseCard("noun", "A concise expression.", "The laconic ended the note."),
        ),
    )
    review.daily_review()

    result = asyncio.run(route(router, request))

    assert result == "Hint: His reply was laconic."
    assert evaluator.calls == []
    pending = review.pending_review()
    assert pending.status is PendingReviewStatus.PENDING
    assert pending.event is not None
    assert pending.event.answer_text is None
    assert pending.entry is not None
    assert pending.entry.last_reviewed is None
```

Add a follow-up contract proving the next response still targets the same entry:

```python
def test_daily_review_answer_after_hint_grades_same_entry(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, review, _ = make_router(tmp_path, evaluator=evaluator)
    capture.capture_entry(
        "laconic",
        (SenseCard("adjective", "Using few words.", "His reply was laconic."),),
    )
    review.daily_review()

    assert asyncio.run(route(router, "give me a hint")) == (
        "Hint: His reply was laconic."
    )
    response = asyncio.run(route(router, "brief or concise"))

    assert response is not None and response.startswith("Grade: Correct")
    assert evaluator.calls[0][0].display_text == "laconic"
    assert evaluator.calls[0][1] == "brief or concise"
```

- [ ] **Step 2: Write failing active-test hint tests**

Add:

```python
@pytest.mark.parametrize("request", HINT_REQUESTS)
def test_active_test_hint_does_not_evaluate_or_advance(
    tmp_path: Path,
    request: str,
) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    before = router._test_service.start().snapshot
    assert before is not None and before.current_question is not None
    question_id = before.current_question.id

    result = asyncio.run(route(router, request))

    assert result == "Hint: Example 0."
    assert evaluator.calls == []
    after = router._test_service.current().snapshot
    assert after is not None and after.current_question is not None
    assert after.current_question.id == question_id
    assert after.current_question.position == 1
    assert after.current_question.answer_text is None
    assert after.summary.correct == 0
    assert after.summary.partial == 0
    assert after.summary.incorrect == 0
```

Add a non-match and outside-study boundary test:

```python
def test_nonmatching_hint_text_is_still_evaluated_during_test(tmp_path: Path) -> None:
    evaluator = FakeEvaluationProvider()
    router, capture, _, _ = make_router(tmp_path, evaluator=evaluator)
    add_test_entries(capture)
    router._test_service.start()

    asyncio.run(route(router, "give me another hint"))

    assert evaluator.calls[0][1] == "give me another hint"
    current = router._test_service.current().snapshot.current_question
    assert current is not None and current.position == 2


def test_hint_phrase_outside_study_flow_uses_capture_route(tmp_path: Path) -> None:
    router, capture, _, provider = make_router(tmp_path)

    result = asyncio.run(route(router, "hint"))

    assert provider.calls == ["hint"]
    assert result is not None
    assert capture.get_entry("hint") is not None
```

- [ ] **Step 3: Run focused routing tests and confirm RED**

Run:

```bash
uv run --extra dev pytest tests/unit/test_gateway_routing.py -k "hint" -q
```

Expected: hint requests are currently sent to the evaluator or capture provider, so the new assertions fail.

- [ ] **Step 4: Implement bounded hint normalization**

In `src/hermes_vocab/hermes_plugin/gateway.py`, import `format_hint` with the existing formatters and add:

```python
_HINT_REQUESTS = frozenset(
    {
        "hint",
        "give me a hint",
        "can i have a hint",
        "show me an example",
        "example sentence",
    }
)


def _is_hint_request(message: str) -> bool:
    normalized = " ".join(message.split()).casefold()
    normalized = normalized.rstrip("?.!").rstrip()
    return normalized in _HINT_REQUESTS
```

Do not strip leading punctuation or classify with the evaluator. The bounded set ensures all other text remains an attempted answer.

- [ ] **Step 5: Route daily-review hints before evaluation**

Replace the status-only review lookup with the read-only snapshot and branch before `complete_pending_review`:

```python
pending = self._review_service.pending_review()
if pending.status is PendingReviewStatus.STORAGE_ERROR:
    return _REVIEW_ERROR
if pending.status is PendingReviewStatus.PENDING:
    if pending.entry is None:
        return _REVIEW_ERROR
    if _is_hint_request(user_message):
        return format_hint(pending.entry)
    completion = await complete_pending_review(
        self._review_service,
        self._evaluation_provider,
        user_message,
    )
    return format_review_completion(completion)
```

Keep pending-review precedence above active-test handling. `complete_pending_review` retains its event-ID concurrency guard for ordinary answers.

- [ ] **Step 6: Route active-test hints before evaluation**

Extend the existing active-test branch:

```python
if test_state.status is TestSnapshotStatus.ACTIVE:
    if (
        test_state.snapshot is None
        or test_state.snapshot.current_question is None
    ):
        return _TEST_ERROR
    if _is_hint_request(user_message):
        return format_hint(test_state.snapshot.current_question.entry)
    completion = await complete_test_question(
        self._test_service,
        self._evaluation_provider,
        user_message,
    )
    return format_test_completion(completion)
```

- [ ] **Step 7: Run routing and formatting suites and confirm GREEN**

Run:

```bash
uv run --extra dev pytest tests/unit/test_gateway_routing.py tests/unit/test_formatting.py -q
```

Expected: all selected tests pass. Existing `show answer`, evaluator-failure, concurrency, daily-review precedence, and post-completion capture tests remain green.

- [ ] **Step 8: Commit state-preserving hint routing**

```bash
git add src/hermes_vocab/hermes_plugin/gateway.py tests/unit/test_gateway_routing.py
git commit -m "Let learners request context without spending an answer" -m "Constraint: Only bounded phrases during an active study flow may bypass evaluation
Rejected: Evaluator-classified intent | nondeterministic and capable of consuming study state
Confidence: high
Scope-risk: moderate
Directive: Preserve pending-review precedence and existing concurrency guards
Tested: uv run --extra dev pytest tests/unit/test_gateway_routing.py tests/unit/test_formatting.py -q"
```

---

### Task 3: Rotate test entries by persisted use history

**Files:**
- Modify: `src/hermes_vocab/test_session.py:137-154`
- Test: `tests/unit/test_test_session.py:66-205`

- [ ] **Step 1: Write the failing unseen-first rotation test**

Add a helper that completes the active session:

```python
def complete_active_session(service: SessionService) -> None:
    for _ in range(5):
        current = service.current().snapshot
        assert current is not None and current.current_question is not None
        service.complete(
            current.current_question.id,
            "answer",
            evaluate(EvaluationGrade.CORRECT),
        )
```

Add:

```python
def test_next_test_prioritizes_entries_never_used_in_a_test(tmp_path: Path) -> None:
    service, database, clock = setup_service(tmp_path)
    add_entries(database, 7)
    first = service.start().snapshot
    assert first is not None
    assert [question.entry.display_text for question in first.questions] == [
        "word-0", "word-1", "word-2", "word-3", "word-4"
    ]
    complete_active_session(service)
    clock.value += timedelta(hours=1)

    second = service.start().snapshot

    assert second is not None
    assert [question.entry.display_text for question in second.questions] == [
        "word-5", "word-6", "word-0", "word-1", "word-2"
    ]
```

- [ ] **Step 2: Write the failing least-recent cycle test**

Add:

```python
def test_test_rotation_cycles_oldest_tested_entries_without_review_mutation(
    tmp_path: Path,
) -> None:
    service, database, clock = setup_service(tmp_path)
    add_entries(database, 10)

    first = service.start().snapshot
    assert first is not None
    assert [question.entry.display_text for question in first.questions] == [
        "word-0", "word-1", "word-2", "word-3", "word-4"
    ]
    complete_active_session(service)
    clock.value += timedelta(hours=1)

    second = service.start().snapshot
    assert second is not None
    assert [question.entry.display_text for question in second.questions] == [
        "word-5", "word-6", "word-7", "word-8", "word-9"
    ]
    complete_active_session(service)
    clock.value += timedelta(hours=1)

    third = service.start().snapshot
    assert third is not None
    assert [question.entry.display_text for question in third.questions] == [
        "word-0", "word-1", "word-2", "word-3", "word-4"
    ]
    with database.connect() as connection:
        scheduling = [tuple(row) for row in connection.execute(
            """
            SELECT last_reviewed, review_status
            FROM vocabulary_entries
            ORDER BY id
            """
        )]
    assert scheduling == [(None, "new")] * 10
```

- [ ] **Step 3: Run focused session tests and confirm RED**

Run:

```bash
uv run --extra dev pytest tests/unit/test_test_session.py -k "rotation or never_used" -q
```

Expected: the current review-priority-only query selects `word-0` through `word-4` for every new session.

- [ ] **Step 4: Add least-recently-tested ordering to the selection query**

Replace the entry selection query in `TestSessionService.start` with:

```sql
SELECT entry.id
FROM vocabulary_entries AS entry
LEFT JOIN (
    SELECT
        question.entry_id,
        MAX(session.started_at) AS last_tested_at
    FROM test_questions AS question
    JOIN test_sessions AS session
        ON session.id = question.session_id
    GROUP BY question.entry_id
) AS test_history
    ON test_history.entry_id = entry.id
WHERE EXISTS (
    SELECT 1
    FROM vocabulary_senses AS sense
    WHERE sense.entry_id = entry.id
)
ORDER BY
    CASE WHEN test_history.last_tested_at IS NULL THEN 0 ELSE 1 END,
    test_history.last_tested_at,
    CASE WHEN entry.last_reviewed IS NULL THEN 0 ELSE 1 END,
    COALESCE(entry.last_reviewed, entry.date_added),
    entry.date_added,
    entry.id
LIMIT ?
```

This uses all persisted sessions, including completed history, and introduces no write or schema change. Keep the existing `BEGIN IMMEDIATE`, insufficient-library behavior, distinct-entry constraints, and five-row insertion unchanged.

- [ ] **Step 5: Run the complete session service suite and confirm GREEN**

Run:

```bash
uv run --extra dev pytest tests/unit/test_test_session.py -q
```

Expected: all tests pass, including restart, duplicate `/test`, concurrency, five-question persistence, and both new rotation contracts.

- [ ] **Step 6: Run database integration coverage**

Run:

```bash
uv run --extra dev pytest tests/integration/test_database.py tests/unit/test_test_session.py -q
```

Expected: all selected tests pass; migration version and lifecycle constraints remain unchanged.

- [ ] **Step 7: Commit test rotation**

```bash
git add src/hermes_vocab/test_session.py tests/unit/test_test_session.py
git commit -m "Rotate practice across the saved vocabulary library" -m "Constraint: Test history must not mutate daily-review scheduling
Rejected: Random selection | permits immediate repeats and weakens reproducibility
Confidence: high
Scope-risk: moderate
Directive: Preserve unseen-first ordering and deterministic review-priority tie-breaks
Tested: uv run --extra dev pytest tests/integration/test_database.py tests/unit/test_test_session.py -q"
```

---

### Task 4: Document and verify the end-to-end behavior

**Files:**
- Modify: `README.md:80-92,202-207`
- Test: `tests/unit/test_gateway_routing.py`
- Test: `tests/unit/test_test_session.py`
- Test: `tests/unit/test_formatting.py`

- [ ] **Step 1: Update learner-facing behavior documentation**

In `README.md`:

1. After the `show answer` paragraph, document that `hint`, `give me a hint`, `can i have a hint`, `show me an example`, and `example sentence` return the first complete stored example during a pending review or active test, without grading or advancing.
2. Replace the statement that `/test` always selects by unchanged daily scheduling order with: never-tested entries first, then least-recently-tested entries, with daily-review priority only as a deterministic tie-breaker.
3. Extend the Telegram smoke flow to request a hint, confirm the word appears in the sentence, answer the same question, finish one test, start another, and confirm unused/least-recently-tested words are preferred.

Use this exact behavior paragraph:

```markdown
During a pending daily review or active test question, `hint`, `give me a hint`, `can i have a hint`, `show me an example`, or `example sentence` returns the first stored example sentence with the vocabulary word visible. Case, repeated whitespace, and trailing `?`, `.`, or `!` are ignored. A hint makes no evaluator request, records no answer or grade, and leaves the same question active.
```

Use this exact rotation sentence:

```markdown
A new test prefers entries never used in a test, then entries used least recently; ordinary daily-review priority breaks ties deterministically. Test history never changes `last_reviewed`, `review_status`, or daily-review scheduling.
```

- [ ] **Step 2: Run all changed-contract tests**

Run:

```bash
uv run --extra dev pytest \
  tests/unit/test_formatting.py \
  tests/unit/test_gateway_routing.py \
  tests/unit/test_test_session.py \
  tests/unit/test_review.py \
  tests/integration/test_database.py \
  tests/integration/test_hermes_plugin.py \
  -q
```

Expected: all selected tests pass with no failures.

- [ ] **Step 3: Run Python diagnostics**

Run LSP diagnostics for `src/**/*.py`.

Expected: no errors or warnings in changed Python files.

- [ ] **Step 4: Run the full suite**

Run:

```bash
uv run --extra dev pytest -q
```

Expected: the complete suite passes.

- [ ] **Step 5: Smoke the installed editable plugin**

Because Hermes is installed editable from this checkout, restart the gateway:

```bash
hermes gateway restart && hermes gateway status
```

Expected: launchd reports the gateway supervised and running. In the configured Telegram root DM:

1. Start or resume `/test`.
2. Send `give me a hint?` and confirm the response starts with `Hint:`, contains the tested word, and does not show a grade or the next question.
3. Answer normally and confirm the same word is graded before question 2 appears.
4. Complete the test, start a new one, and confirm available words omitted from the prior test appear before repeated words.

- [ ] **Step 6: Commit documentation and verification contract**

```bash
git add README.md
git commit -m "Make contextual hints and test rotation operable" -m "Constraint: User guidance must preserve the distinction between test history and review scheduling
Confidence: high
Scope-risk: narrow
Tested: full pytest suite, Python diagnostics, gateway restart, Telegram hint and rotation smoke"
```

---

## Completion Criteria

- Every accepted hint phrase returns the first stored example sentence with the vocabulary word visible.
- Hints never call semantic evaluation, persist answer fields, advance a test, complete a review, or update scheduling.
- An ordinary answer after a hint grades the same pending entry.
- Non-matching text and messages outside study flows retain existing behavior.
- New tests choose never-tested entries first and otherwise choose least-recently-tested entries.
- Rotation is deterministic and leaves daily-review fields and events unchanged.
- Targeted suites, the full suite, Python diagnostics, gateway restart, and Telegram smoke all pass.
