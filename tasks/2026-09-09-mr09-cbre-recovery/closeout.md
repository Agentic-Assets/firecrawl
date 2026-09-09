# MR-R09 CBRE ingest recovery closeout

**Branch:** `fix/mr09-cbre-ingest-lock-recovery`
**Base:** `origin/main` at `a3588714b33c44ee9ab7ec65d7780caf241b7fab`
**Commits:** `215293d79e1780991eec7920a0c7d9ec23fd34ca`, `47aca6e5688f52a5b8ce1ba08484721ee2b76d86`
**Review:** [PR #41](https://github.com/Agentic-Assets/firecrawl/pull/41)
**State:** Code verified; production receipt deferred after a safe host-resource interruption.

## Goal

Recover the MR-R09 producer rollout after CBRE exhausted PostgreSQL's shared
lock table, while preserving atomic publication and fail-closed interruption
recovery.

## What shipped

- Retained one transaction-wide lifecycle advisory lock and deterministic row
  locks while removing unbounded per-listing advisory-lock loops.
- Added exact artifact-run readback so interrupted ingestion resolves to exact
  commit, exact rollback, or ambiguous state before replay.
- Replaced an ambiguous source-index `USING` join exposed by the live replay
  with an explicitly qualified identity predicate.
- Added focused Python coverage and a disposable PostgreSQL contract exercising
  the corrected join across 1,000 rows.

## Verification

- 329 recovery/validation tests passed; one unrelated date-sensitive Colliers
  fixture was deselected.
- 305 focused ingest/lifecycle tests passed on the final code commit.
- The disposable PostgreSQL lifecycle and 1,000-row join contract passed.
- Ruff, Python compilation, and `git diff --check` passed.
- Two independent adversarial reviews reported no actionable findings.
- GitHub reports no configured checks for this PR.

## Decisions

- Preserve one atomic source transaction. Independent chunk commits were
  rejected because they would permit partial publication.
- Fix the application lock protocol instead of increasing the shared Supabase
  project's restart-only `max_locks_per_transaction` setting.
- Require explicit evidence of zero job and generation footprint before replay;
  absence is never inferred from a process exit alone.

## Deferred production proof

Series `2026-09-09T155627Z` completed CBRE and CBRE Deal Flow from code SHA
`47aca6e5688f52a5b8ce1ba08484721ee2b76d86`, then the host CPU guard safely
interrupted JLL after system CPU remained above 55 percent for 10 seconds. No
complete all-source receipt was emitted, so downstream migrations remain
paused. This operational interruption does not invalidate the reviewed code
repair.

