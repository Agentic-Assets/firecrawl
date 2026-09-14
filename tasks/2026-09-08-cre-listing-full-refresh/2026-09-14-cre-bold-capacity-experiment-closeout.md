# CRE bold capacity experiment closeout (2026-09-14)

## Historical preparation snapshot

This section records the reviewed state before activation. It is superseded by
the post-execution closeout below and must not be read as current runtime or PR
state.

**Branch:** `feat/cre-bold-experiment-profile`

**Base:** `origin/main` at `2befd1705b14a173d39fe4bb24d7d9e1a925feb6`

**Reviewed implementation:** `a10743bfc1644f72153b91da0f0f482aeb63b3ee`

**Historical state:** draft PR [#52](https://github.com/Agentic-Assets/firecrawl/pull/52), published and unmerged

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

## Historical operator handoff (superseded below)

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

## Post-execution closeout (2026-09-14)

**Final main:** `0ae92e5249ac5925dfffde173149c65997ff40f1`

**State:** PRs [#52](https://github.com/Agentic-Assets/firecrawl/pull/52),
[#54](https://github.com/Agentic-Assets/firecrawl/pull/54) through
[#60](https://github.com/Agentic-Assets/firecrawl/pull/60) are merged. The
primary checkout is clean at `origin/main`; private runtime evidence remains in
the isolated detached worktree.

### What completed

- PRs #54 and #55 established same-user single-use approval, durable grant and
  lock handling, strict runtime admission, and bounded rollback.
- PR #56 repaired the admission serialization boundary and added an offline
  end-to-end transition, benchmark, and rollback test.
- PR #57 unified RabbitMQ and NuQ settlement parsing against the observed
  RabbitMQ 3.13.7 output. PR #58 corrected Darwin CPU sampling against recorded
  host ticks.
- PR #59 emitted the generated worker as explicit ESM so it can run outside the
  collector package boundary. PR #60 fixed structured JLL detail prices while
  preserving both list and detail hidden-price authority across numeric and
  text fields.
- The live candidate passed 38 admission checks. Replicate 1 processed the
  exact 128-record sample in 38.031 source seconds at maximum local concurrency
  10, a gross diagnostic rate of 201.941 rows per minute. Host CPU peaked at
  52.31 percent across 19 samples with no guard, cooldown, retry, OOM, PID, or
  settlement failure.
- Strict fidelity failed: 103 fresh, 102 native-channel complete, and 89
  structurally complete rows. The all-or-nothing qualified count was zero, so
  the runner stopped before replicates 2 and 3 and produced no adoption
  decision. The result SHA-256 is
  `8c14fcf1944c728e79a54eaae7e509add7cfc09a855759e7992d76ffdf6fe1b0`.
- Independent post-run capture verified the original baseline, idle API,
  RabbitMQ, NuQ, active-crawl and browser-page counters, and an absent canonical
  lock. No database, canonical-cache, listing-status, scheduler, model, or OCR
  write occurred.

### Verification and diagnosis

- Final collector Python suite: `2,780 passed, 18 skipped`.
- Final TypeScript unit suite: `850 passed`; focused JLL suite: `49 passed`;
  TypeScript typecheck and diff checks passed.
- Live execution exposed the ESM launch failure; independent exact-head review
  validated its repair. Independent review then caught and drove closure of a
  hidden-price leak before merge.
- Offline raw-cache diagnosis classified all 25 detail errors as current target
  HTTP 404 pages. Twelve HTTP-200 records exposed structured price objects that
  the detail normalizer previously dropped. One live record lost broker content
  and one lost a brochure. The failed result remains unchanged.

### Decisions

- Preserve the strict all-128 adoption gate. Gross rows and per-record usable
  rows are diagnostics, not qualified throughput.
- Do not replace 404 records silently or infer production listing status from
  the benchmark. A later experiment needs a newly reviewed current cohort and
  the same contemporaneous cohort under baseline and candidate settings.
- Do not rerun on the consumed approval chain. Every later attempt starts with
  a new exact-SHA preflight, approval, admission, grant, and private artifact
  root.
