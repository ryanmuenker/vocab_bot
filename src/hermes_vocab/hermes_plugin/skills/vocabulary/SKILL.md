---
name: vocabulary
description: Capture a single vocabulary word or complete the one pending daily review.
version: 0.1.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [vocabulary, reading, sqlite]
---

# Vocabulary Companion

## When to Use

Use this skill only when the vocabulary plugin's injected turn context says a Telegram message is either a vocabulary capture or the response to a pending review.

## Capture

1. Treat the user's original message as the word. Do not ask for tags, context, a category, or any other metadata.
2. Produce one concise, common English sense with:
   - the word as sent,
   - one part of speech,
   - one short definition,
   - one natural example sentence.
3. Call `vocabulary_save_card` with those four fields.
4. Relay the tool result's `text` value verbatim. Do not add commentary, alternate definitions, or another save claim.
5. If enrichment cannot be produced, apologize concisely and ask the user to resend the word. Never claim it was saved.

## Pending Review

1. Treat the user's original non-command message as raw answer text. `answer` and `show answer` are ordinary answers; do not grade or interpret them specially.
2. Call `vocabulary_complete_review` with the original text.
3. Relay the tool result's `text` value verbatim. Do not score, praise, correct, or add study advice.

## Invariants

- SQLite tool results are the source of truth. Never use Hermes memory or conversation history to decide whether a word exists or a review is pending.
- Never write SQL directly.
- Never emit `✓ Saved.` unless it appears in the successful tool result.
- Unrelated messages remain normal Hermes conversation.
