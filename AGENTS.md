# Production Binding Contract

This contract is binding for every agent working in this repository. The production system is defined by the deployed Cloudflare Worker, not by whichever local implementation is easiest to inspect or modify.

## Production authority

- `worker/` is the only deployed production runtime.
- The deployed `VocabularyCompanion` Durable Object and its SQLite storage are the live data authority.
- Production behavior must be traced through the Worker entry point, routing, Durable Object methods, storage operations, bindings, and deployed configuration.
- A Python code path, Python test result, local process, local SQLite file, snapshot, or local row count is not evidence of production behavior or production state.

## Required implementation order

1. Trace the relevant path in `worker/` first, from the incoming route, webhook, or scheduled event through `VocabularyCompanion`, its SQLite operations, and any external call.
2. Inspect the Worker tests and `worker/wrangler.jsonc` bindings, routes, triggers, variables, and compatibility settings that govern that path.
3. Implement the production change in the Worker first. A Python-only fix does not fix production.
4. Update Python only when migration, reference parity, snapshot interchange, or explicitly requested offline tooling requires it.
5. Verify the Worker locally with the required commands below.
6. If production deployment is authorized, perform a dry run before the real deploy, then verify the deployed version and the affected production path.

## Production evidence

A production claim requires evidence from the deployed Worker. Depending on the change, valid evidence includes the deployed Worker version or deployment record, the active route and bindings, production Worker logs, an observed production request or webhook response, and an authenticated Durable Object admin response or export.

Claims about live entries, cards, reviews, queues, prompts, or any production count must come from authenticated evidence returned by the deployed Worker's `/admin/summary` or `/admin/export` surface. Generic Worker output, an unauthenticated response, Python output, snapshots not fetched from the deployed Worker, and local SQLite counts cannot establish production entries, production counts, migration success, or deployment success.

Always record which deployed version was exercised and what production path was observed. Local Worker execution can verify code before deployment, but it cannot prove that the same version or configuration is live.

### Fast path for authenticated production analysis

Use the admin surfaces according to their actual evidence:

- `/admin/export` returns the complete Durable Object snapshot. `snapshot.reviewAttempts` is the production history for ratings, evaluator grades, answers, before/after scheduling state, review timestamps, and due dates.
- `/admin/inspector-data` is a bounded inspection view with recent attempts per entry.
- `/admin/summary` returns counts, not review history.

First identify the version receiving production traffic and inspect its bindings:

```bash
cd worker
ACTIVE_VERSION_ID="$(npx wrangler deployments list --json | jq -r '.[-1].versions | max_by(.percentage).version_id')"
npx wrangler versions view "$ACTIVE_VERSION_ID" --json
```

Cloudflare secret values are write-only. Wrangler can prove that `ADMIN_TOKEN` is bound, but it cannot recover the token. Do not search local databases, browser storage, shell history, or Cloudflare internals for it, and never ask a user to paste it into chat. Prefer an authenticated response written locally:

```bash
curl -sS https://vocab.ryanmuenker.com/admin/export \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -o /tmp/hermes-vocab-export.json
```

If `/tmp/hermes-vocab-export.json` already exists, validate its envelope and analyze it directly. If the token is unavailable to the agent, ask the user only to create that file, then continue without another design or access handoff. Record the export SHA-256, fetch time, local analysis window, active version ID, and endpoint used. A stale export, local SQLite database, or Python snapshot remains non-production evidence.

## Worker verification

From `worker/`, first run a focused Vitest invocation for the affected path, using the relevant test file and, when useful, test name:

```bash
npx vitest run test/<affected-test-file> -t \"<affected test name>\"
```

After that focused check passes, run the complete Worker suite and typecheck:

```bash
npm test -- --run
npm run typecheck
```

These checks are required but are not deployment evidence. For an authorized production change, also run the deployment dry run, deploy only after reviewing it, and verify the resulting deployed version or deployment record plus the affected live path.

## Configuration safety

- Inspect `worker/wrangler.jsonc` before changing or deploying the Worker. Its routes, Durable Object binding and SQLite class export, triggers, observability, required secrets, variables, compatibility date, and flags are production configuration.
- Preserve the exact `worker/wrangler.jsonc` configuration used for deployment. Never replace it with generated defaults, reconstruct it from memory, or deploy with uncommitted local configuration.
- If configuration changes intentionally, commit the exact deployed `worker/wrangler.jsonc` content alongside the related source change so the repository matches production.
- Run a dry run before every real deployment. Use `npm run deploy:dry-run` from `worker/` and review the resulting configuration and bundle.
- A real deployment, including `npm run deploy` or `npx wrangler deploy`, requires explicit authorization in the current task. Never infer deployment permission from permission to edit code, test, or perform a dry run.
- After an authorized deploy, verify the deployed version or deployment record and confirm that the expected route, Durable Object binding, and configuration are active before claiming production success.

## Python's role

`src/hermes_vocab/`, the root Python tests, scripts, migrations, and local SQLite databases are secondary migration, reference, snapshot, and offline tooling. They may help preserve interchange compatibility or model intended behavior, but they do not serve production traffic and are not the live data authority.

Do not begin a production fix in Python, use Python-only success as closure, or infer live state from local SQLite. When Worker and Python behavior differ, investigate and correct the Worker production path first; change Python afterward only when its limited role requires parity or migration support.
