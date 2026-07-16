# Contextual Multi-Sense Vocabulary Design

**Status:** Approved design, pending written-spec review  
**Date:** 2026-07-16

## Goal

Allow one vocabulary word to accumulate distinct meanings without slowing the normal one-word Telegram capture workflow. A reader may optionally include source context in the same message so Hermes can select the intended sense. Existing words and review history must migrate without data loss.

## Scope

V1.1 adds:

- optional multiline contextual capture;
- multiple senses under one normalized word;
- idempotent handling of repeated meanings;
- concise review reveals containing every stored sense for the selected word;
- migration of every existing vocabulary card into a first sense.

It does not add:

- mandatory context;
- follow-up disambiguation questions;
- sense-level spaced repetition or grading;
- dictionary APIs or external lexical databases;
- automatic replacement or deletion of an existing sense;
- Telegram reply-based corrections.

## User Experience

### Word-only capture

The existing path remains valid:

```text
bank
```

Hermes generates and saves the most likely primary sense.

### Contextual capture

Optional context is sent in one Telegram message:

```text
bank
She sat on the bank and watched the river.
```

The first non-empty line is the candidate word. All remaining non-empty text, preserving internal line breaks, is the source context supplied to Hermes for sense selection.

A contextual capture requires:

- Telegram as the platform;
- no pending review;
- a first line accepted by the existing lexical-word validator;
- at least one non-whitespace character after the first line;
- a message that is not a slash command.

Other multiline messages whose first non-empty line is not one lexical word remain ordinary Hermes conversation. Because the selected syntax uses no explicit prefix, any Telegram message beginning with a standalone lexical line is intentionally treated as contextual capture.

### Responses

A new word uses the existing saved-card response and ends with:

```text
✓ Saved.
```

A distinct sense added to an existing word ends with:

```text
✓ New meaning saved.
```

A repeated sense returns:

```text
Already saved with this meaning.
```

No path asks a follow-up question. Storage errors never claim a save.

## Domain Model

### `vocabulary_words`

Owns spelling identity and word-level review state:

- `id INTEGER PRIMARY KEY`
- `word TEXT NOT NULL`
- `normalized_word TEXT NOT NULL UNIQUE`
- `date_added TEXT NOT NULL`
- `last_reviewed TEXT NULL`
- `review_status TEXT NOT NULL DEFAULT 'new'`

### `vocabulary_senses`

Owns one meaning card:

- `id INTEGER PRIMARY KEY`
- `word_id INTEGER NOT NULL REFERENCES vocabulary_words(id) ON DELETE CASCADE`
- `definition TEXT NOT NULL`
- `part_of_speech TEXT NOT NULL`
- `example_sentence TEXT NOT NULL`
- `source_context TEXT NULL`
- `date_added TEXT NOT NULL`

Indexes support ordered sense retrieval by `(word_id, date_added, id)`.

The database does not impose textual uniqueness on definitions. Semantic equivalence is a language-model decision; string equality would miss paraphrases and incorrectly merge some distinct senses.

### Review events

`review_events.entry_id` becomes `review_events.word_id` and references `vocabulary_words(id)`. Event statuses and one-event-per-local-date semantics remain unchanged.

Review scheduling remains word-level. Adding a sense does not reset `last_reviewed` or create a new review event.

## Migration

Schema migration 002 performs a clean relational conversion in one transaction:

1. Create `vocabulary_words` and `vocabulary_senses` with the new constraints.
2. Copy each existing `vocabulary_entries` row into `vocabulary_words`, preserving its ID, spelling, normalized key, dates, and review status.
3. Create one `vocabulary_senses` row per old entry from its definition, part of speech, and example. `source_context` is null and `date_added` equals the original entry date.
4. Rebuild `review_events` so every event retains its ID, associated word ID, review date, status, timestamps, and answer text.
5. Drop the old tables, recreate indexes, and advance `PRAGMA user_version` to 2.
6. Run foreign-key integrity checking before commit.

If any step fails, the transaction rolls back and schema version 1 remains authoritative.

## Capture Architecture

### Parsing

A small pure parser converts a Telegram message into either:

- no vocabulary capture;
- `CaptureRequest(word, context=None)`;
- `CaptureRequest(word, context=<remaining text>)`.

The parser owns message shape only. It does not generate definitions or inspect SQLite.

Pending review routing retains priority over capture parsing. Therefore, contextual or single-word text is treated as the answer while a review is pending.

### Existing-sense context

Before model inference, the plugin reads existing senses for the normalized word and injects them into the ephemeral model context. The model must classify the requested card as exactly one of:

- `new_word`: no word exists yet;
- `new_sense`: the generated meaning is genuinely distinct from all stored senses;
- `existing_sense`: one stored sense already expresses the same meaning.

For `existing_sense`, the model supplies the matching sense ID. For `new_word` and `new_sense`, it supplies a complete definition, part of speech, and example sentence. The source context is passed separately and must not be invented by the model.

### Tool boundary

The persistence tool receives:

- original word;
- optional source context;
- operation classification;
- optional matching sense ID;
- complete generated card when a write is requested.

Python validates structural invariants:

- normalized word identity;
- operation is allowed for current database state;
- a matching sense belongs to the requested word;
- required card fields are present and bounded;
- source context is bounded;
- a `new_word` cannot be committed when the word already exists;
- a `new_sense` cannot be committed when the word does not exist;
- an `existing_sense` cannot write data.

The model decides semantic equivalence; Python decides authorization and transactional consistency.

### Concurrent captures

Word creation and sense insertion use short `BEGIN IMMEDIATE` transactions. Concurrent attempts to create the same normalized word converge on one `vocabulary_words` row. If database state changes between inference and persistence, the tool rejects the stale operation rather than overwriting or mis-associating data. Hermes may then re-read the current senses and retry once within the same turn.

A repeated semantic sense arriving concurrently may still create two paraphrased rows because SQLite cannot prove semantic equivalence. This is an accepted low-probability V1.1 limitation; adding embeddings or similarity infrastructure would violate the simplicity constraint.

## Review Behavior

Selection and event creation continue to operate on words.

The morning question remains exactly:

```text
What does '<word>' mean?
```

After any non-blank answer or explicit request for the answer, Hermes records completion and reveals all senses in capture order:

```text
1. noun — A financial institution.
   Example: She deposited the cheque at the bank.

2. noun — Land alongside a river.
   Example: They rested on the grassy bank.
```

A one-sense word retains the existing unnumbered definition-and-example format. Review responses are not graded. Existing pending, answered, missed, restart, and same-day retry behavior is unchanged.

## Component Changes

- `models.py`: distinguish `VocabularyWord`, `VocabularySense`, contextual capture requests, and multi-sense results.
- `capture.py`: parse capture messages; load word aggregates; validate state-dependent operations; create words and append senses transactionally.
- `review.py`: select word aggregates and return all associated senses on completion.
- `formatting.py`: format new-sense, repeated-sense, and multi-sense review outcomes deterministically.
- `database.py`: apply migration 002 without adding an ORM.
- `hermes_plugin/hooks.py`: recognize multiline contextual capture and inject existing-sense guidance.
- `hermes_plugin/schemas.py`: extend the save tool contract with operation, context, and matching-sense fields.
- `hermes_plugin/tools.py`: map the extended tool contract to domain outcomes.
- `hermes_plugin/skills/vocabulary/SKILL.md`: teach the model the three operation classifications and prohibit fabricated context.
- `scripts/daily_review.py`: unchanged interface; it still prints deterministic review text.

No Hermes core files are patched.

## Error Handling

- Invalid first line: do not activate vocabulary behavior.
- Empty context after the first line: treat as ordinary one-word capture after whitespace normalization.
- Oversized context or generated fields: return a concise invalid-card response and do not write.
- Unknown matching sense ID or a sense from another word: reject as stale/invalid and do not write.
- State mismatch caused by concurrency: return a retryable conflict, re-read once, then fail safely if still inconsistent.
- SQLite failure: roll back and return the existing storage-error wording.
- Migration failure: preserve version 1 and prevent partial schema use.

## Testing

### Parser contracts

- one lexical line produces a context-free request;
- word plus following lines produces one contextual request;
- leading/trailing blank lines are normalized;
- slash commands and multiline prose whose first non-empty line is not one lexical word do not trigger capture;
- Unicode words, apostrophes, and hyphens retain existing behavior;
- pending review takes precedence over either capture shape.

### Migration contracts

- every old entry becomes one word and one sense;
- IDs, dates, review status, review events, and answers are preserved;
- normalized-word uniqueness remains enforced;
- foreign keys remain valid;
- migration reruns are idempotent;
- a forced mid-migration failure leaves version 1 intact.

### Capture contracts

- a new word creates one word and one sense;
- a different contextual meaning appends a sense without changing word-level review state;
- an existing semantic meaning performs no write;
- an invalid or cross-word sense ID is rejected;
- duplicate concurrent word creation produces one word;
- stale operation classifications do not corrupt state;
- storage failures never produce saved wording.

### Review contracts

- scheduling remains one event per word/date, not per sense;
- one-sense reveal retains the existing format;
- multi-sense reveal is numbered in capture order;
- completing a review updates the word-level review fields once;
- same-day reruns remain silent;
- restart and prior-day missed behavior remain unchanged.

### Hermes integration contracts

- word-only Telegram capture remains unchanged;
- contextual Telegram capture injects the supplied context and existing senses;
- non-Telegram multiline text does not auto-capture;
- the save tool returns deterministic text for new word, new sense, existing sense, invalid state, conflict, and storage error;
- real plugin discovery still registers two tools, one hook, and the bundled skill.

## Acceptance Criteria

- Sending only a word remains a valid sub-ten-second capture.
- Sending a word followed by context saves the contextually intended meaning without a follow-up question.
- Repeating a known meaning does not create another sense.
- A genuinely different meaning appends a sense under the existing word and preserves the original.
- Existing databases migrate without losing cards or review history.
- Morning review remains one word and one question; completion reveals all stored senses concisely.
- SQLite remains the sole authority for word, sense, and review state.
- No external lexical service, ORM, embedding store, or Hermes core patch is introduced.
