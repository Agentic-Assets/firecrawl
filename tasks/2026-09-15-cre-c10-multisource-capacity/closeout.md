# C10 multisource capacity closeout (2026-09-15)

**Branch:** `feat/cre-c10-multisource-capacity`
**Base:** `main` at `db801fa551260e90e6139b6fbfd2d03af0f65048`
**Wave 1 implementation commit:** `16fb3567a14ade2a4b9690fa0c58daa65ec7b643`
**State:** draft [PR #66](https://github.com/Agentic-Assets/firecrawl/pull/66) is open; no merge, runtime, or data mutation occurred.

## Goal

Create a safe, maintainable offline contract for a 20-source C10 capacity
experiment, without adding a generic live collector or changing the established
JLL benchmark path. The branch subsequently added sealed receipt substrate and
source-owned candidate producers; those additions remain offline-only and do
not change the Wave 1 execution boundary.

## What shipped

- `capacity_c10/` is a small, typed package with versioned policy/contracts,
  verified-only adapter admission, immutable v1 cohort binding, serial one-use
  arms, injected settlement/rollback/quarantine hooks, and a pure per-plane
  comparator.
- `cre_capacity_c10_v1.json` fixes the exact 20-source matrix and 12 strict
  detail / 8 authoritative inventory floor. The default registry names every
  source but intentionally admits none.
- `cre_capacity_c10_profiles_v1.json` isolates `c10-p0` and `c10-p1`; the
  central JLL experiment-profile configuration remains unchanged. The generic
  profile parser recognizes the strict C10 workload contract.
- Offline fixtures cover policy and cohort hashing, registry parity, exact
  cohort membership, no-write proof, P0/P1 resource whitelist, counterbalance,
  one-use arms, settlement/rollback/quarantine ordering, and comparison.
- The receipt package now seals private artifacts and request graphs and has
  candidate inventory producers, strict-detail Batch A producers, and one
  Foundry Batch B producer. They describe source-specific cards and parsers;
  no concrete direct-provider transport, CLI/controller integration, registry
  admission, or C10 live run was added. Avison Young, Colliers Main, and the
  remaining Batch B sources remain explicit blockers.

## Wave 1 verification baseline

All results below were obtained on implementation commit `16fb3567a`, before
the later receipt-substrate and producer additions recorded above.

- `python3 -m pytest scripts/firecrawl-ops/cre_collector/tests -q`: 3105
  passed, 20 skipped.
- Focused C10, experiment, multisource, benchmark, runtime, and telemetry suite:
  294 passed, 1 skipped.
- Changed Python: Ruff I/F, Ruff format, `py_compile`, JSON parsing, and
  `git diff --check` passed.
- Existing unchanged collector TypeScript surface on the main checkout:
  `npm run typecheck` passed and unit tests reported 864 passing.

GitHub CI has not been used as completion proof. At the Wave 1 commit, the
worktree lacked its own Node dependencies, so the then-unchanged TypeScript
checks were run from the clean main checkout with its existing dependencies.
That historical baseline is not verification of the later TypeScript receipt
work.

## Decisions made

- Keep C10 profile definitions separate from the central JLL profile file.
  Adding them to the central file made JLL benchmark profile selection
  ambiguous, so isolation preserves existing selection semantics.
- Do not make the v1 cohort hash a shape-only claim. Admission recomputes the
  published multisource-v1 hash recipe before binding cohort membership.
- Do not add a generic adapter, executor, or fallback. Every source must later
  provide reviewed native enumeration, member verification, and attrition
  classification before it can enter a live plan.
- Keep runtime ownership outside this package. A future wiring layer must reuse
  canonical `SharedLock`, capacity runtime transitions, settlement telemetry,
  and quarantine recovery instead of duplicating those safety mechanisms.

## Deliberately deferred

No native live adapter, concrete direct-provider transport, receipt CLI or
controller integration, runtime/resource transition, lock acquisition, source
call, database/cache/status/scheduler/model/OCR change, registry admission, or
experiment arm is included. The request-card and private-artifact seals exist,
but their presence is not execution evidence. These steps require a separately
reviewed integration wave and explicit operator approval.

## Left to the operator

Review and merge draft PR #66 only after normal review gates. Any live C10
run also needs independently reviewed adapters, canonical runtime hook wiring,
fresh operator admission, and the existing production safety gates.
