# Explicit Provider Limit Errors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tell the Telegram learner when OpenCode returned persistent HTTP 429 while preserving the unrecorded, resubmittable review answer.

**Architecture:** Keep HTTP and retry details inside `OpenCodeAdapter`. Its private chat result distinguishes valid content, persistent rate limiting, and generic provider failure; public answer evaluation exposes only a new typed `rate_limited` status. `VocabularyCompanion` maps that domain-facing status to Telegram copy without changing persistence or prompt state.

**Tech Stack:** Cloudflare Workers, Durable Objects with SQLite, TypeScript, Vitest, `@cloudflare/vitest-pool-workers`.

**Worktree constraint:** `worker/src/integrations/opencode.ts` and `worker/test/integrations.test.ts` already contain uncommitted retry/logging work. Preserve those changes and build on them; do not reset or replace either file.

---

### Task 1: Preserve persistent HTTP 429 through the provider adapter

**Files:**
- Modify: `worker/test/integrations.test.ts:45-176`
- Modify: `worker/src/integrations/opencode.ts:22-31,157-277`

- [ ] **Step 1: Write the failing adapter regression test**

Add this test inside `describe("OpenCodeAdapter", ...)` after the transient retry test:

```ts
it("classifies a persistent evaluation rate limit after exhausting retries", async () => {
  const fetchMock = vi.fn().mockResolvedValue(response({}, 429));
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.stubGlobal("fetch", fetchMock);
  const adapter = new OpenCodeAdapter({
    apiKey: "key",
    baseUrl: "https://example.test/v1",
    model: "model",
  });

  expect(await adapter.evaluateAnswer(ENTRY, "stubbornly refusing to change")).toEqual({
    status: EvaluationStatus.RATE_LIMITED,
    evaluation: null,
  });
  expect(fetchMock).toHaveBeenCalledTimes(3);
  expect(warn).toHaveBeenLastCalledWith({
    event: "opencode_chat_failure",
    kind: "http",
    status: 429,
    attempt: 3,
    maxAttempts: 3,
  });
});
```

- [ ] **Step 2: Run the focused test and confirm the missing status fails**

Run from `worker/`:

```bash
npx vitest run test/integrations.test.ts -t "classifies a persistent evaluation rate limit"
```

Expected: FAIL because `EvaluationStatus.RATE_LIMITED` does not exist and persistent 429 currently becomes `provider_error`.

- [ ] **Step 3: Add typed chat and evaluation outcomes**

Extend the public evaluation status:

```ts
export const EvaluationStatus = {
  VALID: "valid",
  INVALID_RESPONSE: "invalid_response",
  RATE_LIMITED: "rate_limited",
  PROVIDER_ERROR: "provider_error",
} as const;
```

Add a private transport result beside the retry constants:

```ts
const ChatStatus = {
  VALID: "valid",
  RATE_LIMITED: "rate_limited",
  PROVIDER_ERROR: "provider_error",
} as const;

type ChatResult =
  | { readonly status: typeof ChatStatus.VALID; readonly content: string }
  | {
      readonly status:
        | typeof ChatStatus.RATE_LIMITED
        | typeof ChatStatus.PROVIDER_ERROR;
      readonly content: null;
    };
```

Change `chat` to return `Promise<ChatResult>`. Preserve the existing request, timeout, logging, and retry policy, but return typed results at each terminal point:

```ts
if (response.ok) {
  const payload = object(await response.json());
  if (payload === null || !Array.isArray(payload.choices)) {
    return { status: ChatStatus.PROVIDER_ERROR, content: null };
  }
  const choice = object(payload.choices[0]);
  const message = choice === null ? null : object(choice.message);
  return message !== null && typeof message.content === "string"
    ? { status: ChatStatus.VALID, content: message.content }
    : { status: ChatStatus.PROVIDER_ERROR, content: null };
}

const retryable = response.status === 429 || response.status >= 500;
if (!retryable || attempt === CHAT_MAX_ATTEMPTS - 1) {
  return {
    status: response.status === 429 ? ChatStatus.RATE_LIMITED : ChatStatus.PROVIDER_ERROR,
    content: null,
  };
}
```

In the `catch`, return a generic provider error on the final attempt and otherwise continue the existing retry delay:

```ts
} catch {
  console.warn({
    event: "opencode_chat_failure",
    kind: "network",
    attempt: attempt + 1,
    maxAttempts: CHAT_MAX_ATTEMPTS,
  });
  if (attempt === CHAT_MAX_ATTEMPTS - 1) {
    return { status: ChatStatus.PROVIDER_ERROR, content: null };
  }
} finally {
  clearTimeout(timeout);
}
```

Keep a defensive generic return after the loop:

```ts
return { status: ChatStatus.PROVIDER_ERROR, content: null };
```

Update `defineEntry` to parse only valid content; both transport failure kinds remain `DefinitionStatus.PROVIDER_ERROR` because this feature changes review grading only:

```ts
const chat = await this.chat(messages, 4_000);
if (chat.status !== ChatStatus.VALID) {
  return { status: DefinitionStatus.PROVIDER_ERROR, cards: [] };
}
const result = parseDefinitionResponse(chat.content);
```

Update `evaluateAnswer` to preserve rate limiting:

```ts
const chat = await this.chat(messages, 4_000);
if (chat.status === ChatStatus.RATE_LIMITED) {
  return { status: EvaluationStatus.RATE_LIMITED, evaluation: null };
}
return chat.status === ChatStatus.PROVIDER_ERROR
  ? { status: EvaluationStatus.PROVIDER_ERROR, evaluation: null }
  : parseEvaluationResponse(chat.content);
```

- [ ] **Step 4: Run all provider integration tests**

Run from `worker/`:

```bash
npx vitest run test/integrations.test.ts
```

Expected: PASS. Existing transient retries, privacy-safe logs, definition parsing, evaluation parsing, and Telegram integration tests remain green.

- [ ] **Step 5: Commit the adapter contract**

Stage only the adapter and its test. This commit intentionally includes the pre-existing retry/logging changes in those same files because typed terminal failures depend on that retry loop.

```bash
git add worker/src/integrations/opencode.ts worker/test/integrations.test.ts
git commit -m "Make provider limits distinguishable after retries" \
  -m "Constraint: Keep answer text and provider bodies out of logs
Rejected: Exposing raw HTTP statuses to study logic | couples review flow to transport details
Confidence: high
Scope-risk: narrow
Directive: Preserve generic handling for non-429 provider failures
Tested: npx vitest run test/integrations.test.ts
Not-tested: Telegram study flow"
```

### Task 2: Send explicit Telegram recovery guidance without recording the answer

**Files:**
- Modify: `worker/test/vocabulary-companion.test.ts:109-153,243-291`
- Modify: `worker/src/vocabulary-companion.ts:61-66,664-720`

- [ ] **Step 1: Make the Worker transport test helper able to return model HTTP failures**

Extend `Transport` and its initial state:

```ts
interface Transport {
  readonly sent: string[];
  readonly modelCalls: () => number;
  telegramFails: boolean;
  modelStatus: number;
  evaluation: { grade: string; feedback: string };
}
```

```ts
const state: Transport = {
  sent,
  modelCalls: () => modelCalls,
  telegramFails: false,
  modelStatus: 200,
  evaluation: { grade: "correct", feedback: "Accurate." },
};
```

Inside the `/chat/completions` branch, return the configured failure before constructing successful model content:

```ts
if (state.modelStatus !== 200) return jsonResponse({}, state.modelStatus);
```


- [ ] **Step 2: Write the failing study-flow regression test**

Add this test after the successful grading test:

```ts
it("explains a provider limit and grades the resubmission after recovery", async () => {
  const io = transport();
  const stub = companion("rate-limited");
  await stub.importSnapshot(library(1));

  await stub.enqueueTelegramUpdate(message(1, "/review"));
  await drain(stub);

  io.modelStatus = 429;
  await stub.enqueueTelegramUpdate(message(2, "a street"));
  await drain(stub);

  expect(io.modelCalls()).toBe(3);
  expect(io.sent[1]).toBe(
    "OpenCode's rate or usage limit was reached. Nothing was recorded. " +
      "Try again after the limit resets or usage is restored — " +
      "the next message you send is graded as your answer.",
  );
  expect((await stub.exportSnapshot())!.answerDrafts).toHaveLength(0);

  io.modelStatus = 200;
  await stub.enqueueTelegramUpdate(message(3, "a street"));
  await drain(stub);

  expect(io.modelCalls()).toBe(4);
  expect(io.sent[2]).toContain("Grade: Correct");
  expect((await stub.exportSnapshot())!.answerDrafts).toHaveLength(1);
});
```

- [ ] **Step 3: Run the focused study-flow test and confirm it fails generically**

Run from `worker/`:

```bash
npx vitest run test/vocabulary-companion.test.ts -t "explains a provider limit"
```

Expected: FAIL because `VocabularyCompanion` still maps every failed evaluation to `EVALUATION_ERROR_REPLY`.

- [ ] **Step 4: Add explicit rate-limit copy and map the typed result**

Add the message beside `EVALUATION_ERROR_REPLY`:

```ts
const EVALUATION_RATE_LIMIT_REPLY =
  "OpenCode's rate or usage limit was reached. Nothing was recorded. " +
  "Try again after the limit resets or usage is restored — " +
  "the next message you send is graded as your answer.";
```

Change the private evaluation helper to return the provider result rather than erasing its status:

```ts
private async evaluateAnswer(
  context: StudyAnswerContext,
  answerText: string,
): Promise<EvaluationResult> {
```

Remove the now-unused `Evaluation` import from `./domain/models`, import `type EvaluationResult` from `./integrations/opencode`, and wrap deterministic answers as valid results:

```ts
return {
  status: EvaluationStatus.VALID,
  evaluation: { grade: EvaluationGrade.INCORRECT, feedback: IDK_FEEDBACK },
};
```

Apply the same wrapper to reverse-card correct and incorrect results, then return the provider result directly:

```ts
return this.provider.evaluateAnswer(context.entry, answerText);
```

In `prepareStudyAnswer`, select the reply before persistence:

```ts
const evaluated = await this.evaluateAnswer(context, answerText);
if (evaluated.status === EvaluationStatus.RATE_LIMITED) {
  return { text: EVALUATION_RATE_LIMIT_REPLY, promptId: null };
}
if (evaluated.status !== EvaluationStatus.VALID || evaluated.evaluation === null) {
  return { text: EVALUATION_ERROR_REPLY, promptId: null };
}
const evaluation = evaluated.evaluation;
```

Leave `recordAnswer` and all later scheduling logic unchanged.

- [ ] **Step 5: Run focused study-flow tests**

Run from `worker/`:

```bash
npx vitest run test/vocabulary-companion.test.ts -t "grades|provider limit"
```

Expected: PASS. The 429 answer remains unrecorded, recovery resubmission is graded, and ordinary grading still works.

- [ ] **Step 6: Commit the learner-facing behavior**

```bash
git add worker/src/vocabulary-companion.ts worker/test/vocabulary-companion.test.ts
git commit -m "Explain provider limits without consuming review answers" \
  -m "Constraint: A failed evaluation must leave the outstanding prompt answerable
Rejected: Generic retry copy for HTTP 429 | hides the action the learner must take
Confidence: high
Scope-risk: narrow
Directive: Never record an answer until evaluation status is valid
Tested: npx vitest run test/vocabulary-companion.test.ts -t 'grades|provider limit'
Not-tested: Full Worker suite"
```

### Task 3: Verify the complete Worker contract

**Files:**
- Verify: `worker/src/integrations/opencode.ts`
- Verify: `worker/src/vocabulary-companion.ts`
- Verify: `worker/test/integrations.test.ts`
- Verify: `worker/test/vocabulary-companion.test.ts`
- Inspect unchanged: `worker/wrangler.jsonc`

- [ ] **Step 1: Run both affected test files together**

Run from `worker/`:

```bash
npx vitest run test/integrations.test.ts test/vocabulary-companion.test.ts
```

Expected: PASS.

- [ ] **Step 2: Run the complete Worker suite**

Run from `worker/`:

```bash
npm test -- --run
```

Expected: PASS with no failed Worker tests.

- [ ] **Step 3: Run Worker typechecking**

Run from `worker/`:

```bash
npm run typecheck
```

Expected: PASS with no TypeScript diagnostics.

- [ ] **Step 4: Exercise the changed study path as a smoke scenario**

Use the focused Durable Object test as the executable smoke scenario:

```bash
npx vitest run test/vocabulary-companion.test.ts -t "explains a provider limit and grades the resubmission after recovery" --reporter=verbose
```

Expected: one passing test proving the learner sees explicit 429 guidance, no draft is recorded during failure, and resubmission succeeds after recovery.

- [ ] **Step 5: Review the production configuration without changing it**

Confirm `worker/wrangler.jsonc` still binds `VOCABULARY`, requires `OPENCODE_API_KEY`, and retains the exact `OPENCODE_BASE_URL` and `OPENCODE_MODEL` values intended for deployment. Do not edit configuration for this feature.

- [ ] **Step 6: Run the deployment dry run**

Run from `worker/`:

```bash
npm run deploy:dry-run
```

Expected: successful bundle/config validation with the custom domain, Durable Object binding, SQLite export, secrets, variables, and cron unchanged.

- [ ] **Step 7: Stop before production deployment**

Do not run `npm run deploy` or `npx wrangler deploy` without explicit authorization in the active conversation. Local tests and a dry run prove the candidate bundle, not production behavior.
