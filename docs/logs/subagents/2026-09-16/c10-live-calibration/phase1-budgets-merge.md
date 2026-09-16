# Phase 1: budgets, cleanup, PR #72 merge

- Commit `a72569de6` on feat/c10-jll-controller-bridge; PR #72 merged with `gh pr merge --merge` as `f965c5f3b`.
- Budgets (old -> new): startup/health 180s -> 600s; per-request 30s -> 90s (Python + TS incl. `receipts/strict_detail/jll.ts`);
  collection margin 60s -> 180s; collection total 570s -> 1710s (derived 17*90+180); teardown 60s -> 180s;
  readiness poll 60s -> 180s; P0/P1 lifecycle deadline 120s -> 600s; worst-case total 815s -> 2495s.
  Unchanged: 8 MiB reply cap (not time), 120s capability expiry cap (still > 90s request), 5s child reap.
- Removed unreachable `_retired_direct_execution` and unused `SharedLock`/`validate_plan` imports; plan_b registry
  rejection test now drives `production._execute_authorized_host_action` and asserts no lifecycle side effects.
- Gates: pytest 3446 passed / 1 skipped (one known load-sensitive reaping test flaked under full suite, passes in
  isolation; orchestrator re-ran host_orchestration/production subset: 79 passed); collector tsc clean; unit 976 passed;
  playwright-service-ts tsc clean, 60 passed; ruff check/format clean; knip hook ran.
