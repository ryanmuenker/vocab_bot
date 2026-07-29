# Reverse Answer Normalization Design

## Problem

Reverse vocabulary reviews compare a learner's answer with the saved entry through `normalize_reverse_answer`. The current normalization case-folds text, collapses whitespace, and removes only trailing `.`, `!`, and `?`. Internal punctuation remains significant, so `pro forma` is incorrectly rejected for the saved entry `Pro-forma`.

## Decision

Canonicalize both submitted answers and saved display text identically:

1. Apply Unicode NFKC normalization.
2. Apply Unicode-aware case folding.
3. Retain only Unicode alphanumeric characters (`str.isalnum`).
4. Compare the resulting canonical strings for exact equality.

This intentionally ignores punctuation and spacing. For example, `pro forma`, `Pro-forma`, and `proforma` are equivalent. The selected broad contract also means punctuation-only distinctions such as `can't` versus `cant`, or `C++` versus `C`, are ignored during reverse review.

## Scope

- Change only deterministic reverse-answer normalization and matching.
- Keep reverse evaluation model-free.
- Do not alter saved entry text, database identity normalization, forward semantic evaluation, rating parsing, or review scheduling.

## Error Handling

The existing study flow rejects blank input before reverse evaluation. A nonblank answer containing no alphanumeric characters canonicalizes to an empty string and remains incorrect for any valid saved entry.

## Verification

Add regression coverage proving:

- `pro forma` matches saved `Pro-forma`.
- Case, repeated whitespace, punctuation, and missing spacing do not affect reverse matching.
- Different alphanumeric content remains incorrect.
- Reverse matching does not invoke the language model.

Run the focused evaluation tests, then the project test suite and static checks required by the repository.
