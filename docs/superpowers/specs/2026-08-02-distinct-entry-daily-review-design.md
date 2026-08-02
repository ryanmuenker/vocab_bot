# Distinct-Entry Daily Review Design

## Problem

Daily review currently budgets unseen work by directional card rather than vocabulary entry. Each entry has one forward card and one reverse card per sense, so a single word can consume multiple slots from the five-per-day introduction quota. The migrated database contains 105 entries, but only 25 have any recorded review attempt. This matches the reported experience: repeated familiar entries while most of the library remains unseen.

The Cloudflare Worker also still uses the pre-fix reverse-answer normalizer. It case-folds and collapses whitespace but preserves internal punctuation, so `pro forma` does not match the saved entry `Pro-forma` in the live bot even though the Python implementation was already corrected.

## Decisions

### Daily review selection

- Preserve FSRS due-first ordering.
- Preserve carryover of unanswered cards from an explicitly exited review.
- Treat the daily introduction quota as five distinct vocabulary entries, not five directional cards.
- When constructing a new review queue, select at most one card per entry among newly selected due and unseen candidates. Previously skipped carryover remains unchanged.
- Continue ordering unseen entries by card creation time and stable ID so selection remains deterministic.
- Do not cap the total due backlog or move unseen entries ahead of due entries.

This means one multi-sense entry consumes one daily introduction slot and appears at most once in the newly selected portion of a review queue. Its other direction remains scheduled for a later review. A queue inherited from an explicit `/endstudy` may retain older duplicate-entry carryover until those cards are completed.

### Reverse-answer identity

Mirror the approved Python contract in the Worker:

1. Apply Unicode NFKC normalization.
2. Apply the existing deterministic Unicode case-fold implementation.
3. Retain only Unicode letters and numbers.
4. Compare the canonical submitted answer with the canonical saved display text.

Spacing and punctuation are intentionally insignificant: `pro forma`, `Pro-forma`, and `proforma` match. The broad contract also treats punctuation-only distinctions such as `can't`/`cant` and `C++`/`C` as equivalent during reverse review.

## Runtime parity

Python remains the behavioral source of truth. Update both implementations and both test suites in the same change:

- `src/hermes_vocab/review.py`
- `src/hermes_vocab/hermes_plugin/evaluation.py` only if parity has drifted (the Python matcher is already correct)
- `worker/src/storage/vocabulary-store.ts`
- `worker/src/domain/routing.ts`

No database migration is required. Existing card state, due times, attempts, and introduction dates remain valid.

## Verification

Regression coverage must prove:

- A review library with forward and reverse cards introduces five distinct entries, not five cards.
- The newly selected portion of a review queue contains at most one card per entry.
- Due entries remain ahead of unseen entries.
- The per-local-day introduction quota counts distinct entries in both runtimes.
- Worker reverse review accepts `pro forma` for saved `Pro-forma` without invoking the evaluator.
- Python and Worker focused tests, full suites, lint, and type checking pass.

After local verification, deployment is a separate production action. Confirm the production Worker version only after an authorized deploy.
