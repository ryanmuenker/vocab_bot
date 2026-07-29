# Reverse Answer Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Count reverse-review answers as correct when they differ from the saved entry only in case, spacing, or punctuation.

**Architecture:** Keep deterministic reverse matching in `hermes_plugin/evaluation.py`. Canonicalize both operands with Unicode NFKC normalization, case folding, and an alphanumeric-only filter; preserve the existing evaluation and scheduling flow.

**Tech Stack:** Python 3.12, `unicodedata`, pytest, Ruff

---

### Task 1: Lock and implement punctuation-insensitive reverse matching

**Files:**
- Modify: `tests/unit/test_evaluation.py:436-469`
- Modify: `src/hermes_vocab/hermes_plugin/evaluation.py:1-7,151-153`

- [ ] **Step 1: Write the failing regression tests**

Add the reported hyphen case and the approved broad normalization cases to the existing parameterized reverse-evaluation test, and update the direct normalizer assertion:

```python
@pytest.mark.parametrize(
    ("answer", "expected_grade"),
    [
        (" pro   forma. ", EvaluationGrade.CORRECT),
        ("PRO FORMA!!!", EvaluationGrade.CORRECT),
        ("pro-forma", EvaluationGrade.CORRECT),
        ("proforma", EvaluationGrade.CORRECT),
        ("pro/forma", EvaluationGrade.CORRECT),
        ("pro form", EvaluationGrade.INCORRECT),
        ("projected statement", EvaluationGrade.INCORRECT),
        ("obdurate", EvaluationGrade.INCORRECT),
    ],
)
def test_reverse_answer_is_normalized_exactly_without_model(
    tmp_path: Path,
    answer: str,
    expected_grade: EvaluationGrade,
) -> None:
    service, _, _ = answerable_study(tmp_path, direction=CardDirection.REVERSE)
    provider = valid_provider(EvaluationGrade.CORRECT)

    result = asyncio.run(continue_study_answer(service, provider, answer))

    assert result.context.draft.evaluation.grade is expected_grade
    assert result.context.sense is not None
    assert result.context.sense.id == 12
    assert len(result.context.entry.senses) == 2
    assert provider.calls == []
    assert result.status is (
        StudyAnswerStatus.AWAITING_RATING
        if expected_grade is EvaluationGrade.CORRECT
        else StudyAnswerStatus.FINALIZED
    )
```

```python
def test_reverse_normalizer_and_rating_parser_are_state_deterministic() -> None:
    assert normalize_reverse_answer("  Pro-forma...  ") == "proforma"
    assert normalize_reverse_answer("can't") == normalize_reverse_answer("cant")
    assert normalize_reverse_answer("C++") == normalize_reverse_answer("C")
    assert parse_rating(
        " GOOD ",
        (ReviewRating.HARD, ReviewRating.GOOD, ReviewRating.EASY),
    ) is ReviewRating.GOOD
    assert parse_rating(
        "good",
        (ReviewRating.AGAIN, ReviewRating.HARD),
    ) is None
    assert allowed_ratings(EvaluationGrade.INCORRECT) == ()
```

- [ ] **Step 2: Run the focused tests and confirm the regression fails**

Run:

```bash
uv run pytest tests/unit/test_evaluation.py::test_reverse_answer_is_normalized_exactly_without_model tests/unit/test_evaluation.py::test_reverse_normalizer_and_rating_parser_are_state_deterministic -q
```

Expected: the new `pro-forma`, `proforma`, or `pro/forma` correct cases fail because the current normalizer preserves internal punctuation and spacing.

- [ ] **Step 3: Implement the canonical reverse normalizer**

Import `unicodedata` and replace `normalize_reverse_answer` with:

```python
def normalize_reverse_answer(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if character.isalnum())
```

- [ ] **Step 4: Run focused verification**

Run:

```bash
uv run pytest tests/unit/test_evaluation.py::test_reverse_answer_is_normalized_exactly_without_model tests/unit/test_evaluation.py::test_reverse_normalizer_and_rating_parser_are_state_deterministic -q
```

Expected: all selected tests pass, including `pro-forma`, and the fake evaluation provider remains unused.

- [ ] **Step 5: Run repository verification**

Run:

```bash
uv run pytest -q
uv run ruff check src tests
```

Expected: the full Python suite passes and Ruff reports no errors.

- [ ] **Step 6: Commit the behavior change**

Stage only the implementation and regression test:

```bash
git add src/hermes_vocab/hermes_plugin/evaluation.py tests/unit/test_evaluation.py
git commit
```

Use the repository Lore commit protocol. The subject should explain that equivalent written forms now receive credit; record the broad punctuation-insensitive constraint and the exact verification commands in trailers.
