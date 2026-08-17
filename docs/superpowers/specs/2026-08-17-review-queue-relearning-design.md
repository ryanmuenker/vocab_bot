# Review Queue and Relearning Design

## Problem

The production review history from 15–17 August 2026 shows two independent sources of repetition:

- All 30 newly introduced cards were reverse cards for already-practiced entries. None introduced an untouched vocabulary entry. Another 236 unintroduced reverse cards remain on 113 practiced entries.
- Lapsed cards return directly to the next daily queue. Hermes deliberately omits short learning and relearning steps, and daily review disables the queue's existing one-retry mechanism. Cards with very low stability and difficulty near 10 can therefore remain on one-day intervals even after a later successful rating.

The scheduler works per directional card, while Telegram presents a word-level experience. An Easy rating can move one card away while another card for the same entry appears the next day. Production history contained nine next-day entry repeats after Easy; eight used a different sibling card.

## Decisions

### Separate introduction lanes

Preserve due-first FSRS ordering, carryover ordering, the ten-card daily introduction maximum, and one newly selected card per entry. Divide the daily introduction budget into two lanes whenever both have candidates:

- Seven untouched entries. An entry is untouched when it has no review attempt. Its forward card is introduced first.
- Three unseen sibling cards for entries with review history. Existing deterministic creation-time and card-ID ordering chooses among siblings.

Unused capacity transfers to the other lane, so the total remains at most ten. The 7/3 split is a preference, not a reason to leave available capacity unused. Directional tests remain unseen-only and continue to bypass daily introduction accounting.

Daily accounting must classify the card at its original review introduction. Exiting and resuming a review on the same local day must not consume another slot or change its lane.

### One daily-review relearning retry

Use the existing queue retry representation rather than adding timers, alarms, or a second scheduler:

1. A first Again during daily review applies the normal FSRS transition and appends one retry for the same card at the queue tail.
2. The user-visible schedule reports that the retry was added.
3. Hard, Good, or Easy on the retry applies normal same-day FSRS math and completes the card.
4. Again on the retry does not append another retry. Its effective due is floored at the next local midnight when that is later than the raw due.
5. Entry sibling burial, immutable answer drafts, immutable review attempts, and compare-and-set finalization remain unchanged.

An explicit exit ends the one-session retry opportunity; the first Again transition has already scheduled the card for the following day, so an exited retry is not carried into a new session. A local-day rollover of an open session may reorder ordinary work, but it must retain any still-queued retry relationship and never create a second retry.

### Preserve FSRS Easy behavior

Do not add an Easy-specific interval floor or alter the FSRS parameters. The universal one-day minimum remains valid for a low-stability, high-difficulty card. Add a regression that proves such an Easy transition can still produce one day, preventing a future symptom-level workaround.

Desired retention remains 0.9. Tuning retention or adding a daily due cap is outside this change.

### Production evidence fast path

Update the binding root `AGENTS.md` and `worker/README.md` with a short authenticated-analysis workflow:

- `/admin/export` is the full production-history surface; `snapshot.reviewAttempts` contains ratings, grades, before/after schedule state, timestamps, and due dates.
- `/admin/inspector-data` is bounded to recent per-entry inspection, and `/admin/summary` contains counts only.
- Wrangler can confirm that `ADMIN_TOKEN` exists but cannot recover its value.
- Prefer an authenticated export written to `/tmp/hermes-vocab-export.json`; never ask a user to paste the token into chat.
- Identify the active deployed version with Wrangler before making production claims.
- Never substitute Python output, local SQLite, or stale snapshots for the authenticated Worker export.

## Runtime authority and data compatibility

The production change is confined to the Worker:

- `worker/src/storage/vocabulary-store.ts`
- `worker/src/domain/scheduling.ts` only if a regression exposes an actual math defect; no math change is intended
- `worker/src/vocabulary-companion.ts` only if retry messaging needs integration changes
- `worker/test/`
- `worker/wrangler.jsonc` remains unchanged

No database or snapshot migration is required. Existing cards, attempts, queue retry references, due times, and introduction dates remain valid. Python is unchanged because it does not serve production and no snapshot contract changes.

## Verification

Regression coverage must prove:

- With both lanes populated, daily review introduces seven untouched forward cards and three known-entry sibling cards after due work.
- Either lane borrows unused capacity without exceeding ten total introductions.
- Repeated exits and starts on one local day do not spend the lane budgets twice.
- A first daily-review Again appends exactly one tail retry.
- A successful retry completes without another retry; a second Again schedules tomorrow without another retry.
- Low-stability Easy can still produce the universal one-day minimum.
- Existing due-first, distinct-entry, sibling-burial, rollover, snapshot, and directional-test behavior remains intact.

Run focused Worker tests first, then the complete Worker suite and typecheck. Deployment is a separate production action and is not authorized by this design.
