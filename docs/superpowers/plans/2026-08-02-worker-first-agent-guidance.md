# Worker-First Agent Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every future agent treat the Cloudflare Worker and Durable Object database as the production authority.

**Architecture:** A root `AGENTS.md` supplies binding repository instructions. Current operational READMEs and the active 2026-08-02 design artifacts will use the same Worker-first terminology; older historical plans remain untouched.

**Tech Stack:** Markdown repository guidance

---

### Task 1: Add the binding Worker-first agent contract

**Files:**
- Create: `AGENTS.md`

- [ ] **Step 1: Write the root contract**

Create `AGENTS.md` with these sections and rules:

```markdown
# Hermes Vocabulary Repository Contract

## Production authority

- `worker/` is the primary product and the only deployed production runtime.
- The `VocabularyCompanion` Durable Object and its SQLite storage are the authority for live entries, cards, schedules, sessions, prompts, attempts, and inbox state.
- `~/.local/share/hermes-vocab/vocabulary.sqlite3` is a stale, independent pre-cutover database unless a task explicitly performs a fresh export/import. Never use it for current production counts or behavior claims.

## Required implementation order

1. Trace user-visible behavior through `worker/src/index.ts`, `worker/src/vocabulary-companion.ts`, `worker/src/domain/`, `worker/src/storage/`, and `worker/wrangler.jsonc`.
2. Reproduce and fix the Worker path first.
3. Add or update focused tests under `worker/test/`.
4. Run the full Worker suite and TypeScript checking.
5. Update Python only when migration, snapshot interchange, offline tooling, or deliberately maintained parity requires it.

A Python-only change is never a production fix. Never claim a live issue is resolved from Python tests or the local SQLite database.

## Production evidence

- Query live counts and state only through authenticated Worker admin/export surfaces or another direct Durable Object production probe.
- Treat Telegram output as evidence of user-visible behavior, not as the database authority.
- When production changes, verify the deployed Worker version and `/healthz`; use an authenticated provider/admin smoke path when the changed behavior depends on external credentials.

## Worker verification

From `worker/`, run focused Vitest coverage, then:

```bash
npm test -- --run
npm run typecheck
```

Run `npm run deploy:dry-run` for deployment/configuration changes. Production deployment still requires explicit authorization.

## Configuration safety

- `worker/wrangler.jsonc` is production configuration.
- Inspect and preserve concurrent edits before deploying.
- Confirm the deployment output uses the intended model, bindings, variables, custom domain, and cron trigger.
- Commit and push the exact configuration that was deployed.

## Python's role

`src/hermes_vocab/`, `tests/`, and the local SQLite database are retained for migration, snapshot compatibility, offline tooling, historical reference, and optional parity coverage. They are secondary to Worker correctness and must not redirect a production task away from `worker/`.
```

- [ ] **Step 2: Read the contract standalone**

Confirm it answers runtime authority, data authority, implementation order, verification commands, deployment evidence, configuration safety, and Python's limited role without requiring another document.

---

### Task 2: Correct current operational documentation

**Files:**
- Modify: `README.md:1-55`
- Modify: `worker/README.md:1-13`

- [ ] **Step 1: Replace the root README introduction**

Rewrite the opening before the existing local architecture diagram:

```markdown
# Hermes Vocabulary Companion

The production vocabulary companion runs in `worker/`: a Cloudflare Worker receives Telegram webhooks, a `VocabularyCompanion` Durable Object owns SQLite state and scheduling, and a cron trigger drives daily review delivery.

> **Production authority:** `worker/` is the primary implementation and the Durable Object database is the live source of truth. The Python/Hermes implementation under `src/hermes_vocab/` and `~/.local/share/hermes-vocab/vocabulary.sqlite3` are retained for migration, offline tooling, historical reference, and optional parity work; they do not automatically receive entries saved by the Worker.

See [`worker/README.md`](worker/README.md) for current deployment, operations, and migration guidance.

## Legacy local architecture
```

Keep the existing local architecture diagram after the new `## Legacy local architecture` heading.

- [ ] **Step 2: Qualify the root SQLite statement**

Replace the unqualified source-of-truth paragraph with:

```markdown
Within the legacy local runtime, its SQLite database is authoritative for that independent deployment. It is not synchronized with the production Durable Object and must not be used for current production counts or scheduling claims.
```

- [ ] **Step 3: Correct the Worker README authority statement**

Replace lines 12–13 with:

```markdown
The Worker implementation under `worker/src/` is the production source of truth. User-visible fixes must land here first and be covered under `worker/test/`. Python is secondary migration/reference code; update it only when snapshot compatibility, offline tooling, or deliberately maintained parity requires it.
```

---

### Task 3: Correct active design artifacts

**Files:**
- Modify: `docs/superpowers/specs/2026-08-02-distinct-entry-daily-review-design.md:33-40`
- Modify: `docs/superpowers/plans/2026-08-02-distinct-entry-daily-review.md:5-9`

- [ ] **Step 1: Correct the active design specification**

State that Worker behavior is production authority and Python parity is secondary:

```markdown
## Runtime authority

The Cloudflare Worker is the production source of truth. Update and verify Worker storage and routing first:

- `worker/src/storage/vocabulary-store.ts`
- `worker/src/domain/routing.ts`
- `worker/test/`

Update Python only to preserve deliberate parity for migration and offline tooling:

- `src/hermes_vocab/review.py`
- `src/hermes_vocab/hermes_plugin/evaluation.py`
- `tests/`
```

- [ ] **Step 2: Correct the active implementation plan header**

Replace its Architecture line with:

```markdown
**Architecture:** The Worker is the production source of truth. Fix and verify Worker review selection and reverse matching first; update Python second only to preserve deliberate migration/offline parity.
```

---

### Task 4: Verify and publish the guidance

**Files:**
- Verify: `AGENTS.md`, `README.md`, `worker/README.md`, and the two active 2026-08-02 artifacts

- [ ] **Step 1: Search current operational docs for contradictions**

Search the files above for `Python.*source of truth`, `local-first`, and unqualified local SQLite authority. Expected: no statement tells agents that Python or local SQLite controls production.

- [ ] **Step 2: Confirm documentation-only scope**

Inspect the change summary. Expected: only Markdown files changed; no runtime, tests, configuration, or production data changed.

- [ ] **Step 3: Commit and push**

Stage only the guidance files. Use the repository Lore commit protocol and push `feat/hermes-vocabulary` so future agents receive the contract.
