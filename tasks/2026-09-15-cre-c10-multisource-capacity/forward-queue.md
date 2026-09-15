# Forward queue after C10 multisource capacity Wave 1 (2026-09-15)

Candidate work surfaced during Wave 1. This is a menu, not a roadmap; verify
each item before acting.

## Hardening

- **Build an FD/inode-bound private execution seal** (priority: highest;
  confidence: verified requirement)
  Bind raw receipt files, native provider identities, canonical targets,
  adapter and source revisions, and ordered request cards before an arm can
  execute. The public cohort intentionally omits private paths and cannot be a
  live request ledger by itself.

- **Implement source-native adapter fixtures in reviewed families** (priority:
  highest; confidence: verified gap)
  Add adapters only after native enumeration and member evidence can reject
  total, pagination, identity, canonical-URL, and truncation drift. Retain a
  source-specific attrition classifier; HTTP status alone is not enough.

- **Wire the serial protocol to canonical runtime primitives** (priority:
  highest; confidence: verified requirement)
  Use `SharedLock`, runtime preflight/transition, established settlement
  telemetry, verified P0 rollback, and quarantine recovery through a narrow
  integration layer. Do not migrate those controls into `capacity_c10`.

## Robustness

- **Add execution-card accounting and family-stop evidence** (priority: high;
  confidence: verified requirement)
  A future arm should prove every planned card began and ended exactly once,
  with zero fallback/retry substitution. Challenge, throttle, parser, fidelity,
  or unknown failures should invalidate comparison and retain affected-family
  evidence.

- **Add private-artifact path-race tests** (priority: high; confidence:
  hypothesis grounded in existing safety model)
  Test replaced, stale, symlinked, and mismatched private receipt artifacts
  before implementing the execution seal.

## Simplification

- **Keep C10 profiles and JLL profiles independently versioned** (priority:
  medium; confidence: verified)
  Preserve the separation unless a future profile-registry API can prove exact
  experiment selection without central-file ambiguity.

## Evaluation

- **Specify timestamp and matched-pair freshness limits before live comparison**
  (priority: high; confidence: verified gap)
  Include execution order, arm identity, maximum pairing gap, and source-level
  fresh evidence so P0/P1 deltas cannot be attributed to capacity when source
  state changed between arms.
