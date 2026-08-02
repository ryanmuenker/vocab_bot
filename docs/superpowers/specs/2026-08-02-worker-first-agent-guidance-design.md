# Worker-First Agent Guidance Design

## Problem

The repository contains two implementations of vocabulary behavior:

- the deployed Cloudflare Worker under `worker/`, backed by Durable Object SQLite;
- the earlier local Python/Hermes implementation under `src/hermes_vocab/`, backed by a separate local SQLite file.

Current documentation is contradictory. `worker/README.md` calls Python the source of truth, then later states that the Durable Object is authoritative after cutover. The root README still opens as a local-first product. This has caused agents to implement and verify fixes only in Python, inspect the stale local database as if it were production, and leave live Worker behavior unchanged.

## Decision

Add a root `AGENTS.md` as the binding repository contract for future coding agents. It will declare:

1. `worker/` is the primary product and production runtime.
2. The production Durable Object SQLite database is the live data authority.
3. User-visible bugs must be traced through the Worker webhook, `VocabularyCompanion`, Worker domain/storage code, and deployed configuration first.
4. A Python-only change is not a production fix.
5. Shared behavior changes must update Worker code and Worker tests in the same change; Python parity is secondary and must not delay or replace the Worker fix.
6. Production counts and state claims must come from authenticated Worker admin/export surfaces, not `~/.local/share/hermes-vocab/vocabulary.sqlite3`.
7. Worker verification requires focused Vitest coverage, the full Worker suite, TypeScript checking, and deployment/version evidence when production is changed.
8. `worker/wrangler.jsonc` is production configuration. Agents must inspect and preserve concurrent edits and verify the bindings used by deployment.
9. Python remains useful for migration, snapshot interchange, historical reference, and optional parity tests, but its local database is not synchronized after cutover.

## Documentation changes

- Create `AGENTS.md` at the repository root.
- Rewrite the root README introduction and source-of-truth wording to describe the current Worker production architecture before the legacy local runtime.
- Replace the contradictory Worker README statement that Python is the source of truth.
- Correct the active 2026-08-02 distinct-entry spec and plan so they no longer teach Worker-second implementation.
- Leave older historical plans unchanged. They document earlier architecture and are explicitly subordinate to the current root agent contract and current README.

## Scope boundaries

- Do not delete the Python implementation.
- Do not migrate or mutate production data.
- Do not add a synchronization service between local and Worker databases.
- Do not add source-text tests for documentation wording.
- Do not change runtime behavior in this documentation-only task.

## Verification

- Read the final `AGENTS.md` as a standalone instruction set and confirm it answers runtime authority, data authority, implementation order, verification, and deployment configuration.
- Search current operational documentation for unqualified claims that Python or local SQLite is the production source of truth.
- Confirm historical documents are not rewritten wholesale.
- Confirm the change contains documentation only.
