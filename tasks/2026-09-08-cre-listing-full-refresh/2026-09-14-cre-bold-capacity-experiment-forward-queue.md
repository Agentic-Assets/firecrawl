# Forward queue after CRE bold capacity preparation (2026-09-14)

Candidate work surfaced during this session. This is a menu, not a roadmap.
Verify each item before acting.

## Hardening

- **Add a supported matched-baseline admission path** (priority: high;
  confidence: verified gap). Build a coordinating-reviewed baseline producer for the
  exact same 128-record, three-replicate evidence contract. Until then, keep
  the comparator's adoption result hard-disabled.
- **Add a guarded quarantine-recovery command** (priority: high; confidence:
  verified operational gap). Replace manual lock-directory removal with an
  operator command that proves every local settlement counter idle, displays
  the retained evidence, and clears only the exact canonical interlock.
- **Upgrade vulnerable pinned HTTP dependencies in a separate PR** (priority:
  high; confidence: verified dependency debt). The 2026-09-14 collector audit
  reported three existing production dependency advisories: one moderate and
  two high, affecting `axios`, `undici`, and the pinned Firecrawl SDK dependency
  chain. Keep that dependency upgrade and its regression testing isolated from
  this operational benchmark change.

## Robustness and simplification

- **Fault-inject the real review-helper lifecycle on macOS** (priority: medium;
  confidence: test expansion). In a disposable operator-owned directory, test
  signal, timeout, lost-response, invalid-grant, and cleanup-error paths without
  touching Docker or provider endpoints.
- **Consolidate embedded review-helper protocol code** (priority: medium;
  confidence: design hypothesis). The controller and benchmark intentionally
  embed small stdlib-only helpers for isolated atomic consumption, not Unix
  privilege separation. Evaluate a single audited protocol module or generated
  helper only if it preserves exact source binding and does not create a
  reusable authorization service.
- **Add CI coverage for cross-language retry and lock contracts** (priority:
  medium; confidence: verified local-only coverage). Run Python lock/admission
  tests plus the pinned SDK transport-attempt test in a dependency-complete CI
  lane.

## Evaluation

- **Run the first coordinating-reviewed 128-detail candidate** (priority: operator
  decision; confidence: implementation prepared). Capture all three replicates,
  settlement, resource, cooldown, and per-record fidelity evidence. A negative
  or inconclusive result is acceptable and must not be converted into a
  production success claim.
- **Consider a larger soak only after candidate safety passes** (priority:
  later; confidence: planned). Preserve the same no-write, source identity,
  freshness, fidelity, retry, and settlement contract.
- **Evaluate the later two-provider 6+4 split** (priority: later; confidence:
  proposed). Keep the global page ceiling at 10 and the writer serial; do not
  treat 10 as a per-provider allocation.

## Post-execution queue (2026-09-14)

The first candidate produced a safe negative result. These are candidate next
steps, not authorization for another live run.

### Evaluation

- **Build a current exact 128-record JLL cohort** (priority: high; confidence:
  verified need). Validate current HTTP-200 property identity before freezing
  the sample, retain explicit 404 attrition evidence separately, and never
  mutate production status from benchmark observations.
- **Add a supported matched-baseline producer** (priority: high; confidence:
  verified gap). Run the same contemporaneous cohort and evidence contract at
  baseline and candidate settings. Until then, keep adoption disabled.
- **Repeat only under a fresh authorization chain** (priority: operator
  decision; confidence: verified requirement). Use the merged structured-price
  repair, exact clean SHA, new private artifacts, and the unchanged guard and
  no-write boundaries.

### Observability and hardening

- **Expose individually complete rows as a diagnostic** (priority: medium;
  confidence: verified usability gap). Add an explicitly named per-record count
  while preserving `qualified_fresh_unique_rows=0` whenever any exact-cohort
  quality error exists.
- **Admit the real worker dependency and import chain** (priority: medium;
  confidence: verified operational gap). Make the no-write preflight prove the
  lockfile-installed `tsx` executable and generated ESM worker import before a
  single-use approval can be issued.
- **Classify target 404 attrition separately** (priority: medium; confidence:
  verified measurement gap). Report current source unavailability apart from
  parser, transport, and fidelity errors without weakening source identity or
  silently swapping cohort members.

### Deferred

- **Do not tune the 90-percent guard from this run** (priority: later;
  confidence: insufficient evidence). One 38-second replicate peaking at 52.31
  percent is useful safety evidence, not a basis for higher limits or a soak.
- **Do not attempt the 6+4 provider split yet** (priority: later; confidence:
  blocked by missing matched evidence). First establish current-cohort JLL
  fidelity and a supported baseline comparison.
