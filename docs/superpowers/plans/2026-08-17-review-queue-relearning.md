# Review Queue and Relearning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reserve daily review capacity for genuinely new words, add one tail relearning retry after Again, preserve FSRS Easy math, and document the fastest authenticated production-analysis path.

**Architecture:** Keep the existing per-card FSRS and snapshot schemas. Split unseen daily-review candidates into untouched-forward and attempted-entry sibling lanes inside `VocabularyStore.selectCards`, using persisted queue/session history to account for same-day quota consumption. Reuse the existing `study_queue.retry_of_queue_item_id` path by enabling one retry in review mode; no timer, alarm, migration, or scheduling-formula change.

**Tech Stack:** Cloudflare Workers, Durable Objects SQLite, TypeScript, Vitest with `@cloudflare/vitest-pool-workers`, Wrangler.

---

### Task 1: Lock the 7/3 Introduction Contract

**Files:**
- Modify: `worker/test/storage.test.ts:281-450`
- Modify: `worker/src/storage/vocabulary-store.ts:52-53,1542-1647`

- [ ] **Step 1: Replace the sibling-first regression with a 7/3 lane regression**

Create three practiced multi-sense entries, complete their forward cards, add ten untouched entries, then start the following day's review. Assert the queue contains seven untouched forward cards, three practiced-entry reverse cards, then borrowed untouched capacity only when a lane is short:

```ts
expect(queue.slice(0, 7).every((item) =>
  item.card.direction === CardDirection.FORWARD && untouchedIds.has(item.card.entryId)
)).toBe(true);
expect(queue.slice(7, 10).every((item) =>
  item.card.direction === CardDirection.REVERSE && practicedIds.has(item.card.entryId)
)).toBe(true);
```

Retain the existing sixteen-untouched-entry test to prove untouched cards borrow all three unused sibling slots. Add a practiced-only case with ten entries to prove sibling cards borrow all seven unused untouched slots.

- [ ] **Step 2: Run the focused selection tests and confirm failure**

Run:

```bash
cd worker
npx vitest run test/storage.test.ts -t "introduces|borrows"
```

Expected: the mixed-pool test fails because current selection orders attempted siblings before untouched entries; the practiced-only borrowing case exposes the single undifferentiated quota.

- [ ] **Step 3: Add separate lane constants and persisted daily counts**

Replace the single policy constant with explicit totals:

```ts
const DAILY_INTRODUCTION_LIMIT = 10;
const DAILY_UNTOUCHED_ENTRY_LIMIT = 7;
const DAILY_SIBLING_CARD_LIMIT = 3;
```

Query review queue rows introduced on the local day. Classify each original introduction by whether its entry had a review attempt before that queue row's session started, and count distinct entry IDs per lane. This keeps exit/resume rows from spending a lane twice.

- [ ] **Step 4: Partition and select unseen candidates**

Keep due and optional weak cards unchanged. For daily review, partition unintroduced cards into:

```ts
const untouched = unseen cards where !attemptedEntries.has(entryId) && direction === "forward";
const siblings = unseen cards where attemptedEntries.has(entryId);
```

Append candidates with one shared distinct-entry set:

1. Up to the remaining 7-slot untouched allocation.
2. Up to the remaining 3-slot sibling allocation.
3. Borrow remaining total capacity from untouched candidates.
4. Borrow any still-remaining total capacity from sibling candidates.

Explicit `onlyUnseen` directional tests retain their existing creation-order behavior and bypass daily lane accounting.

- [ ] **Step 5: Run focused storage selection tests**

Run:

```bash
npx vitest run test/storage.test.ts -t "VocabularyStore selection"
```

Expected: all selection tests pass, including due-first ordering, 7/3 mixing, both borrowing directions, same-day quota accounting, sibling progression, and directional-test isolation.

### Task 2: Add One Daily-Review Tail Retry

**Files:**
- Modify: `worker/test/storage.test.ts:548-640`
- Modify: `worker/test/vocabulary-companion.test.ts:380-428`
- Modify: `worker/src/storage/vocabulary-store.ts:1257-1264`

- [ ] **Step 1: Replace the no-retry storage regression**

Change the daily-review test to assert a first Again appends exactly one retry:

```ts
expect(first.result.transition!.retrySameSession).toBe(true);
expect(first.result.snapshot!.queue.filter(
  (item) => item.retryOfQueueItemId !== null,
)).toHaveLength(1);
expect(first.result.snapshot!.status).toBe(StudySessionStatus.ACTIVE);
```

Finalize that retry with Good and assert the session completes, no second retry exists, and review attempts carry `is_same_session_retry` values `[0, 1]`. Add a second case where the retry is Again; assert no third queue item appears and the resulting due is at least one day after the retry.

- [ ] **Step 2: Add a companion-level review regression**

Drive `/review` through delivery, an incorrect answer, and the automatically applied Again. Assert the Telegram response contains both `Rated: Again` and `Retry added at the end.`, then contains the retry prompt after ordinary queued cards.

- [ ] **Step 3: Run focused retry tests and confirm failure**

Run:

```bash
npx vitest run test/storage.test.ts test/vocabulary-companion.test.ts -t "daily review|tail retry"
```

Expected: failure because `VocabularyStore.finalize` currently passes `allowSameSessionRetry: false` for review sessions.

- [ ] **Step 4: Enable the existing retry mechanism for review**

Remove the review-mode exception and allow the transition to append its one retry:

```ts
const result = transition(before, rating, now, {
  sameSessionRetry: retryAgain,
  dueFloorUtc: retryAgain ? this.nextLocalMidnight(now) : null,
  allowSameSessionRetry: true,
});
```

Keep `retryAgain` tied to `retry_of_attempt_id !== null`, so only a retry Again receives the due floor and `transition` never appends a third attempt.

- [ ] **Step 5: Run focused storage and companion retry tests**

Run the command from Step 3. Expected: all selected retry tests pass and the integration response exposes the existing retry message.

### Task 3: Preserve the Universal One-Day Easy Minimum

**Files:**
- Modify: `worker/test/scheduling.test.ts:118-191`
- No production scheduler change intended

- [ ] **Step 1: Add the low-stability Easy regression**

Construct the observed class of deeply lapsed schedule and rate it Easy:

```ts
const card = createCardSchedule({
  state: CardScheduleState.REVIEW,
  stability: 0.15,
  difficulty: 9.92,
  due: NOW,
  lastReview: plusDays(NOW, -1),
  repetitions: 6,
  lapses: 5,
});
const result = transition(card, ReviewRating.EASY, NOW);
expect(result.rawDue).toEqual(plusDays(NOW, 1));
expect(result.effectiveDue).toEqual(result.rawDue);
```

This is a preservation test: it must pass against current production math and fail if a future Easy-specific floor is introduced.

- [ ] **Step 2: Run the focused scheduler test**

Run:

```bash
npx vitest run test/scheduling.test.ts -t "low-stability Easy"
```

Expected: pass without production code changes.

### Task 4: Document the Authenticated Production Fast Path

**Files:**
- Modify: `AGENTS.md:21-28`
- Modify: `worker/README.md:43-80`

- [ ] **Step 1: Add binding agent guidance**

Under `Production evidence`, document:

```bash
cd worker
ACTIVE_VERSION_ID="$(npx wrangler deployments list --json | jq -r '.[-1].versions | max_by(.percentage).version_id')"
npx wrangler versions view "$ACTIVE_VERSION_ID" --json
curl -sS https://vocab.ryanmuenker.com/admin/export \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -o /tmp/hermes-vocab-export.json
```

State that `/admin/export` contains full `snapshot.reviewAttempts`, `/admin/inspector-data` is bounded recent inspection, and `/admin/summary` is counts only. Cloudflare secret values are write-only; do not waste time trying to recover `ADMIN_TOKEN` through Wrangler and never ask for it in chat. If the token is unavailable to the agent, request the local export file directly.

- [ ] **Step 2: Add the operator command to the Worker README**

Add a concise `Authenticated production inspection` subsection using the same export target. State that local SQLite and older snapshots are not production evidence.

- [ ] **Step 3: Check guidance for contradictions and placeholders**

Search the modified guidance for `TBD`, `TODO`, placeholder hosts, or commands that conflict with `worker/wrangler.jsonc`. Expected: no matches and the production host is `vocab.ryanmuenker.com`.

### Task 5: Complete Worker Verification

**Files:**
- Verify: `worker/test/storage.test.ts`
- Verify: `worker/test/vocabulary-companion.test.ts`
- Verify: `worker/test/scheduling.test.ts`
- Verify: `worker/src/storage/vocabulary-store.ts`
- Verify: `AGENTS.md`
- Verify: `worker/README.md`

- [ ] **Step 1: Run focused affected tests**

```bash
cd worker
npx vitest run test/storage.test.ts -t "VocabularyStore selection|daily review"
npx vitest run test/vocabulary-companion.test.ts -t "tail retry"
npx vitest run test/scheduling.test.ts -t "low-stability Easy"
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the complete Worker suite**

```bash
npm test -- --run
```

Expected: all Worker tests pass.

- [ ] **Step 3: Run Worker typecheck**

```bash
npm run typecheck
```

Expected: TypeScript exits successfully with no diagnostics.

- [ ] **Step 4: Smoke the review progression path**

Run the companion-level retry test without a name filter alongside its real delivery/evaluation flow. Confirm the observed messages include the first Again schedule, `Retry added at the end.`, intervening prompt delivery, and the eventual retry prompt.

- [ ] **Step 5: Review deployment boundary**

Confirm `worker/wrangler.jsonc` is unchanged. Do not run `npm run deploy` or `npx wrangler deploy`; production deployment requires separate explicit authorization.
