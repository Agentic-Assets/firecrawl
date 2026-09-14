# CRE bold capacity experiment closeout (2026-09-14)

**Branch:** `feat/cre-bold-experiment-profile`

**Base:** `origin/main` at `2befd1705b14a173d39fe4bb24d7d9e1a925feb6`

**Reviewed implementation:** `a10743bfc1644f72153b91da0f0f482aeb63b3ee`

**State:** draft PR [#52](https://github.com/Agentic-Assets/firecrawl/pull/52), published and unmerged

## Goal

Prepare an opt-in, no-write JLL capacity experiment without changing the
production profile, scheduler, runtime, database, model routing, or collector
defaults. Make any later resource transition and 128-detail run fail closed on
independent authorization, exact runtime identity, bounded work, settlement,
fidelity, and artifact proof.

## What shipped

- `c0a685418` through `cd673256d`: centralized and validated the named
  `bold-jll-128` and unchanged `production-current` profiles, planner, and
  runtime preflight contracts.
- `69d2c414b` and `36b88ee54`: added the dry-run-first runtime controller,
  no-write JLL benchmark, runbook, and discoverable entrypoint.
- `a10743bfc`: hardened one-use root approval and grant handling, partial-state
  compensation, SDK retry bounds, evidence and comparison schemas, baseline
  provenance, and crash-durable mutual exclusion for Python and shell lock
  users.

## Verification

- Full CRE collector Python suite: `2,739 passed, 18 skipped`.
- Focused capacity, checkpoint, and shell suite: `510 passed`.
- Ruff, capacity-file format checks, `py_compile`, shell syntax, Bun
  transpilation, and `git diff --check`: passed.
- Fresh clean-HEAD runtime preflight at `a10743bfc`: all 27 checks passed;
  browser and API remained exact baseline in apply dry-run.
- Offline sample: exactly 128 JLL records selected from 10,996 parseable cached
  rows; inventory SHA-256
  `37e10a320692209850d21c99b59527d36e820231ee0de6dfcb71cda7620443d1`.
- Multi-lens adversarial review found and drove fixes for authorization,
  compensation, retry, evidence, baseline, and crash-lock defects. Final
  skeptical review reported no remaining confirmed finding.
- Collector TypeScript verification against the pinned dependency tree:
  typecheck passed and all 848 tests passed. The retry telemetry test now
  explicitly proves that backoff is recorded only before real retries, never
  after the terminal attempt.
- GitHub has no reported CI check rollup on the draft PR as of closeout.

## Decisions

- Keep `production-current` as the implicit default. The bold profile remains
  explicit and opt-in.
- Separate the machine-generated transition receipt, independent root approval,
  runtime admission, and root-owned benchmark grant. Local JSON alone is not
  execution authority.
- Arm the canonical lock before a detached worker can start. Unknown settlement
  and abrupt parent death preserve a non-reclaimable operator-recovery stop.
- Preserve one global 10-page budget and JLL width 10. The later 6+4
  two-provider split remains unimplemented.
- Hard-disable adoption until a supported, exactly matched baseline producer
  exists. A safe candidate result may provide diagnostic evidence but cannot
  authorize adoption.

## Deliberately deferred

- No candidate resource transition, provider call, live benchmark, larger soak,
  matched baseline run, or two-provider experiment was performed.
- No production write canary is permitted by this work. The historical global
  duplicate-URL regression and the broader source/readback gates remain
  separate.

## Left to the operator

Review the draft PR and decide whether to authorize a fresh root-reviewed
candidate attempt. Any live attempt requires a new clean-HEAD receipt, the
independently created root approval, current `sudo` authorization, exact sample,
idle runtime, and explicit `--run`. Merge, activation, and later experiment
progression remain operator decisions.

## Superseded approval mechanism (2026-09-14)

The Unix-root and `sudo` mechanism described above was later found to be an
implementation-only constraint, not a company, product, or repository policy.
The follow-up repair replaces it with a non-root operating-account-owned
private file and same-user one-use grant while retaining independent
coordinating review, exact bindings, expiry, atomic consumption, rollback, and
all benchmark gates. See the current runbook for the supported procedure.
