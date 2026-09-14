# Forward queue after CRE bold capacity preparation (2026-09-14)

Candidate work surfaced during this session. This is a menu, not a roadmap.
Verify each item before acting.

## Hardening

- **Add a supported matched-baseline admission path** (priority: high;
  confidence: verified gap). Build a root-reviewed baseline producer for the
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

- **Fault-inject the real root helper lifecycle on macOS** (priority: medium;
  confidence: test expansion). In a disposable root-owned directory, test
  signal, timeout, lost-response, invalid-grant, and cleanup-error paths without
  touching Docker or provider endpoints.
- **Consolidate embedded root-helper protocol code** (priority: medium;
  confidence: design hypothesis). The controller and benchmark intentionally
  embed small stdlib-only helpers for privilege separation. Evaluate a single
  audited protocol module or generated helper only if it preserves exact source
  binding and does not create a reusable authorization service.
- **Add CI coverage for cross-language retry and lock contracts** (priority:
  medium; confidence: verified local-only coverage). Run Python lock/admission
  tests plus the pinned SDK transport-attempt test in a dependency-complete CI
  lane.

## Evaluation

- **Run the first root-reviewed 128-detail candidate** (priority: operator
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
