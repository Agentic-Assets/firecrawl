# AGENTIC-1229 CRE quality repair closeout

**Branch:** `fix/agentic-1229-cre-quality-repair`
**Integrated main:** `4c4cfafefbbf81c8dcddeea0751fb9c4cf473815`
**Verified code candidate:** `b26a5279fb43d762540d44f8e6434939cc5a1f38`
**Review:** [PR #44](https://github.com/Agentic-Assets/firecrawl/pull/44)
**State:** Code and database contracts verified; full production source refresh remains a separate operational proof.

## What changed

- Hardened exact inventory generation, checkpoint validation, and CBRE convergence against partial or moving provider snapshots.
- Preserved JLL and JLL Investor children only when strict live identity and detail provenance prove an authoritative detail observation.
- Added the Colliers main-site browser batch path with a dedicated one-session capacity limit and fail-closed Chromium cleanup permits.
- Made the exact Colliers derived-PSF correction append a deterministic reconciliation job, price-change event, and watched-field history snapshot in the same transaction.
- Integrated current `main` without discarding its MR-R09 run-identity, lifecycle-lock, or rollback protections.

## Verification

- Collector Python suite: 2,216 passed, 1 skipped.
- Collector TypeScript suite: 814 passed.
- Playwright sidecar suite: 11 passed; TypeScript build passed.
- Disposable PostgreSQL 16 migration 016 and lifecycle contract passed, including the 1,000-row reconciliation shape.
- Ruff, Python compilation, collector typecheck, Prettier, and diff checks passed.
- Independent finder and skeptic reviews confirmed four risks; all code risks were repaired and the PR/base exact-head requirement is enforced in the release steps.

## Boundaries

- No production schema or listing mutation was performed during this code closeout.
- Passing these gates does not certify that all 51 production source passes have completed.
- If the earlier Colliers one-row correction was applied before this history fix, live readback must determine whether a provenance repair is required. Do not replay the parent-row mutation blindly.
