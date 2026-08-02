# Distinct-Entry Daily Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce five distinct vocabulary entries per local day, prevent duplicate entries in newly selected review work, and make the live Worker accept punctuation-equivalent reverse answers.

**Architecture:** Python remains the source of truth. Its review start path will enable the selector's existing distinct-entry mode; the Worker will mirror that behavior and count introduced entries rather than card rows. Worker reverse-answer normalization will mirror Python's NFKC, case-fold, alphanumeric-only canonicalization.

**Tech Stack:** Python 3.12, SQLite, pytest, TypeScript, Cloudflare Durable Objects, Vitest, Ruff, TypeScript compiler

---

### Task 1: Lock Python review selection to distinct entries

**Files:**
- Modify: `tests/unit/test_review.py:187-210`
- Modify: `src/hermes_vocab/review.py:105-113`

- [ ] **Step 1: Write the failing Python regression test**

Replace the current single-card unseen fixtures in `test_start_snapshots_all_due_plus_five_unseen_and_restart_resumes` with six entries that each have forward and reverse cards. Assert that the selected unseen portion contains the first card from five distinct entries and that ten sibling card rows are marked introduced:

```python
new_entry_cards: list[tuple[int, int]] = []
for index in range(6):
    entry_id = 100 + index
    forward_id = add_card(
        database,
        entry_id=entry_id,
        card_id=2_000 + index * 2,
        state=CardScheduleState.NEW,
        due=NOW,
        direction=CardDirection.FORWARD,
        created_at=NOW + timedelta(seconds=index),
    )
    reverse_id = add_card(
        database,
        entry_id=entry_id,
        card_id=2_001 + index * 2,
        state=CardScheduleState.NEW,
        due=NOW,
        direction=CardDirection.REVERSE,
        sense_id=3_000 + index,
        created_at=NOW + timedelta(seconds=index, microseconds=1),
    )
    new_entry_cards.append((forward_id, reverse_id))

started = service.start()
restarted = ReviewService(database, TIMEZONE, lambda: NOW).start()

expected_unseen = [forward for forward, _ in new_entry_cards[:5]]
assert [item.card.id for item in started.snapshot.queue] == due_ids + expected_unseen
assert len({item.card.entry_id for item in started.snapshot.queue}) == len(started.snapshot.queue)
assert restarted.snapshot == started.snapshot
with database.connect() as connection:
    assert connection.execute(
        "SELECT COUNT(DISTINCT entry_id) FROM vocabulary_cards "
        "WHERE introduced_local_date = '2026-07-20'"
    ).fetchone()[0] == 5
    assert connection.execute(
        "SELECT COUNT(*) FROM vocabulary_cards "
        "WHERE introduced_local_date = '2026-07-20'"
    ).fetchone()[0] == 10
```

- [ ] **Step 2: Run the focused Python test and confirm it fails**

Run:

```bash
uv run pytest tests/unit/test_review.py::test_start_snapshots_all_due_plus_five_unseen_and_restart_resumes -q
```

Expected: FAIL because `ReviewService.start` currently allows forward and reverse siblings from the same entry into the newly selected queue.

- [ ] **Step 3: Enable distinct-entry selection in Python review start**

Pass `distinct_entries=True` to the existing `_select_cards` call:

```python
cards = carryover + self._select_cards(
    connection,
    now=now,
    distinct_entries=True,
    excluded_ids={card.id for card in carryover},
)
```

Do not change carryover construction or due-first sorting.

- [ ] **Step 4: Run focused Python verification**

Run:

```bash
uv run pytest tests/unit/test_review.py::test_start_snapshots_all_due_plus_five_unseen_and_restart_resumes tests/unit/test_review.py::test_selector_supports_direction_distinct_entries_and_shared_daily_quota -q
```

Expected: PASS.

---

### Task 2: Mirror distinct-entry review selection in the Worker

**Files:**
- Modify: `worker/test/storage.test.ts:152-190`
- Modify: `worker/src/storage/vocabulary-store.ts:481-505,1345-1401`

- [ ] **Step 1: Write the failing Worker storage regression**

Update the unseen quota test to express entry-level behavior for `seedEntries(store, 6)`, whose entries each own forward and reverse cards:

```typescript
it("introduces five distinct unseen entries per local day across sessions", async () => {
  await runInDurableObject(stub(), (_instance, state) => {
    const store = new VocabularyStore(state.storage, "UTC");
    seedEntries(store, 6);

    const started = store.startReview(new Date("2026-07-20T10:00:00Z"));
    expect(started.snapshot!.queue.map((item) => item.card.id)).toEqual([1, 3, 5, 7, 9]);
    expect(new Set(started.snapshot!.queue.map((item) => item.card.entryId)).size).toBe(5);
    expect(rows<{ count: number }>(
      state.storage,
      "SELECT COUNT(DISTINCT entry_id) AS count FROM vocabulary_cards " +
        "WHERE introduced_local_date = '2026-07-20'",
    )[0]!.count).toBe(5);
    expect(rows<{ count: number }>(
      state.storage,
      "SELECT COUNT(*) AS count FROM vocabulary_cards " +
        "WHERE introduced_local_date = '2026-07-20'",
    )[0]!.count).toBe(10);

    expect(store.exitStudy(new Date("2026-07-20T10:05:00Z")))
      .toBe(StudyMutationStatus.COMPLETED);
    const second = store.startReview(new Date("2026-07-20T11:00:00Z"));
    expect(second.snapshot!.queue.map((item) => item.card.id)).toEqual([1, 3, 5, 7, 9]);
  });
});
```

- [ ] **Step 2: Run the focused Worker storage test and confirm it fails**

Run:

```bash
npm --prefix worker test -- --run test/storage.test.ts -t "introduces five distinct unseen entries"
```

Expected: FAIL because review selection currently chooses card IDs `[1, 2, 3, 4, 5]` and counts introduced card rows.

- [ ] **Step 3: Implement Worker entry-level selection**

In `startReview`, request distinct entries from `selectCards`:
```typescript
const cards = [
  ...carryover.cards,
  ...this.selectCards(now, { excludedIds, distinctEntries: true }),
];
```

Count introduced vocabulary entries rather than directional card rows:

```typescript
const introducedToday =
  oneOrNull(
    this.sql.exec<{ count: number }>(
      "SELECT COUNT(DISTINCT entry_id) AS count FROM vocabulary_cards " +
        "WHERE introduced_local_date = ?",
      today,
    ),
  )?.count ?? 0;
```

The existing `entries` set and `distinctEntries` branch then enforce one newly selected card per entry while preserving due-first ordering.

- [ ] **Step 4: Run focused Worker storage verification**

Run:

```bash
npm --prefix worker test -- --run test/storage.test.ts -t "VocabularyStore selection"
```
Expected: PASS.

---

### Task 3: Mirror punctuation-insensitive reverse matching in the Worker

**Files:**
- Modify: `worker/test/vocabulary-companion.test.ts:367-385`
- Modify: `worker/src/domain/routing.ts:73-77`

- [ ] **Step 1: Write the failing live-path regression**

Change the reverse-study fixture entry's display text to `Pro-forma`, answer with `pro forma`, and assert correct evaluation without an evaluator call. Also add direct canonicalization expectations if the test file already imports `normalizeReverseAnswer`:

```typescript
expect(normalizeReverseAnswer("  Pro-forma...  ")).toBe("proforma");
expect(normalizeReverseAnswer("can't")).toBe(normalizeReverseAnswer("cant"));
expect(normalizeReverseAnswer("C++")).toBe(normalizeReverseAnswer("C"));
```

The companion-level assertion remains:

```typescript
expect(io.modelCalls()).toBe(0);
expect(io.sent[1]).toContain("Grade: Correct\nFeedback: Exact match to the saved entry.");
expect(io.sent[1]).toContain("Answer: Pro-forma");
```

- [ ] **Step 2: Run the focused Worker reverse test and confirm it fails**

Run:

```bash
npm --prefix worker test -- --run test/vocabulary-companion.test.ts -t "grades a reverse card"
```

Expected: FAIL because the current Worker normalizer preserves the internal hyphen.

- [ ] **Step 3: Implement the Worker canonical normalizer**
Replace `normalizeReverseAnswer` with NFKC, deterministic case folding, and a Unicode letter/number filter:

```typescript
export function normalizeReverseAnswer(text: string): string {
  const folded = caseFold(text.normalize("NFKC"));
  return Array.from(folded)
    .filter((character) => /[\p{Letter}\p{Number}]/u.test(character))
    .join("");
}
```

Keep hint and rating normalization unchanged.

- [ ] **Step 4: Run focused Worker reverse verification**

Run:

```bash
npm --prefix worker test -- --run test/vocabulary-companion.test.ts -t "grades a reverse card"
```
Expected: PASS with zero evaluator calls.

---

### Task 4: Verify parity and repository health

**Files:**
- Verify only; do not modify unrelated concurrent changes such as `worker/wrangler.jsonc`.

- [ ] **Step 1: Run full Python verification**

```bash
uv run pytest -q
uv run ruff check src tests
```

Expected: all Python tests pass and Ruff reports `OK`.

- [ ] **Step 2: Run full Worker verification**

From `worker/`:

```bash
npm test -- --run
npm run typecheck
```

Expected: all Vitest tests pass and TypeScript reports no errors.

- [ ] **Step 3: Verify the reported reverse pair directly**

Exercise `normalizeReverseAnswer("pro forma")` and `normalizeReverseAnswer("Pro-forma")` through the Worker test path and confirm both canonicalize to `proforma`.

- [ ] **Step 4: Commit the implementation**

Stage only the Python/Worker implementation, tests, and this plan. Exclude unrelated concurrent files. Use the repository Lore commit protocol, recording Python and Worker verification in `Tested:`.

- [ ] **Step 5: Request production deployment authorization**

Deployment changes the external Worker and is not implied by local implementation. After all local verification passes, ask whether to run `npx wrangler deploy`; if authorized, deploy and verify the new version appears in `npx wrangler deployments list`.
