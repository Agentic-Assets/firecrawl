# C10 production integration closeout (2026-09-15)

**Branch:** `feat/c10-production-integration`
**Base:** `feat/c10-v3-host-session` at `86235ad5fbc13029f0ea49e5cf8c3079df5fe554`
**State:** canonical-ledger and deadline hardening verified locally. No runtime,
Docker, provider, or database calls occurred.

## Goal

Remove the callback-injected C10 coordinator authority gap and make the sole
production path compose the durable claim, canonical runtime lifecycle,
canonical lock, sealed host execution, and authenticated terminalization.

## What shipped

- Removed `runner.run_one_coordinated_arm` and all callback hook types. The
  remaining runner module is data-only and cannot trigger external work.
- Added one production coordinator that claims before runtime or host activity,
  performs C10 preflight, applies and rolls back P1 under the same canonical
  lock, proves settlement, quarantines failures, and writes an exclusive
  terminal record.
- Removed the public host-child callback. Tests now replace only the host's
  private child implementation.
- Added an authenticated-host terminal validator. JLL host evidence is not
  inflated into a 20-source comparator result.
- Added a dry-run-default CLI with guarded one-arm smoke and fixed
  counterbalanced-sequence modes.
- Follow-up review hardening derives the sole ledger path from the canonical
  lock, plan, and session; terminal state atomically includes the safe
  authenticated envelope; and splits host ownership into focused modules.

## Verification

- `ruff check` and `ruff format --check` on the six changed Python files:
  passed.
- `python3 -m py_compile` on changed C10 Python modules: passed.
- Focused C10 Python tests: `32 passed` after the follow-up hardening.
- `git diff --check`: passed.
- TypeScript unit tests could not start in this isolated worktree because its
  `node_modules` lacks `tsx`. No TypeScript source changed.

## Decisions made

- Terminalize only authenticated host evidence. The current host proves a
  sealed JLL lane, not all twenty planned sources, so comparison remains
  explicitly non-comparable rather than inventing rows for missing sources.
- Reuse `cre_capacity_runtime` for profile transitions and idle proof instead
  of duplicating runtime commands in the C10 package.

## Left to the operator

Review the pushed branch. A live run remains separately gated by fresh
operator approvals and canonical runtime preflight.
