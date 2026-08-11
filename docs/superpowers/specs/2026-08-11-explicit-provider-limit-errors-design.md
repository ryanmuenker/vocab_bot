# Explicit Provider Limit Errors Design

## Problem

The production Worker retries transient OpenCode failures, then collapses every failed evaluation into the same learner-facing message. When OpenCode returns HTTP 429 because a rate or usage limit was reached, Telegram says only that it could not evaluate the answer. The learner cannot distinguish a quota problem from malformed model output or a temporary network failure.

## Decision

Preserve a typed `rate_limited` outcome from the OpenCode transport through answer evaluation to the study flow. After the existing retry policy is exhausted on HTTP 429, Telegram will say:

> OpenCode's rate or usage limit was reached. Nothing was recorded. Try again after the limit resets or usage is restored — the next message you send is graded as your answer.

Other provider, network, and invalid-response failures retain the existing generic evaluation-error reply.

## Data Flow

1. `OpenCodeAdapter` performs the existing bounded request and retry sequence.
2. A successful response returns its model content as before.
3. Persistent HTTP 429 returns a typed rate-limit result rather than the generic provider-error result.
4. `evaluateAnswer` exposes that distinction through `EvaluationStatus` without returning transport details or learner-facing copy.
5. `VocabularyCompanion` selects the explicit rate/usage-limit reply while leaving the current prompt outstanding.

## Invariants

- No answer, grade, rating, or review attempt is recorded when evaluation fails.
- The outstanding prompt remains answerable, and the next ordinary message is graded as the answer.
- HTTP 429 is retried exactly as it is today before the explicit message is sent.
- Raw provider response bodies, credentials, vocabulary entries, and learner answers are not logged or shown.
- No storage schema, snapshot format, scheduling behavior, Python tooling, or deployed configuration changes.

## Alternatives Rejected

- Expose raw HTTP status codes to study logic: this leaks transport concerns across the integration boundary.
- Generate Telegram copy inside `OpenCodeAdapter`: this couples the provider adapter to one presentation channel.
- Label every HTTP 429 as exhausted usage: 429 may also represent a temporary rate limit, so the message names both cases.

## Verification

Add Worker regression coverage proving:

- three persistent HTTP 429 responses produce the typed rate-limit evaluation status;
- other HTTP and network failures remain generic provider errors;
- a rate-limited review answer sends the explicit Telegram reply;
- the answer is not recorded and the outstanding prompt remains ready for resubmission;
- the next submitted answer is evaluated normally once the provider recovers.

Run the focused integration and `VocabularyCompanion` tests, then the complete Worker suite and typecheck required by `AGENTS.md`. A production deployment requires a dry run and explicit authorization before deploying.
