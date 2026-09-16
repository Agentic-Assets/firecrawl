# Forward queue after C10 multisource capacity Wave 1 (2026-09-15)

Candidate work surfaced during Wave 1. This is a menu, not a roadmap; verify
each item before acting.

## Hardening

- **Operationalize the existing private receipt and request-graph seals**
  (priority: highest; confidence: verified requirement)
  The package now seals private artifacts, native-provider identities,
  canonical targets, coordinator-declared adapter/source identifiers, and
  ordered request-card graph evidence. Wave 1 now fingerprints the complete
  collector source/config dependency surface, but it does not supply concrete
  host/browser transport or private receipt admission. Before an arm can
  execute, a separate reviewed integration must bind the existing seals to the
  FD/inode-safe execution boundary and real provider transport. The public
  cohort intentionally omits private paths and cannot be a live request ledger
  by itself.

- **Populate the fail-closed repository admission authority** (priority:
  highest; confidence: verified requirement)
  The checked-in authority intentionally approves no cohort, complete plan, or
  adapters.
  Only after private receipt review should a separate reviewed commit pin one
  exact cohort digest, the exact derived plan digest, and all twenty source-tree
  implementation digests. Never derive approval from caller-provided flags,
  version labels, or a self-hashed public cohort.

- **Implement source-native adapter fixtures in reviewed families** (priority:
  highest; confidence: verified gap)
  Add adapters only after native enumeration and member evidence can reject
  total, pagination, identity, canonical-URL, and truncation drift. Retain a
  source-specific attrition classifier; HTTP status alone is not enough.

- **Wire the serial protocol to canonical runtime primitives** (priority:
  highest; confidence: verified requirement)
  The library seam already uses `SharedLock`, runtime preflight/transition,
  settlement telemetry, verified P0 rollback, and quarantine recovery. A
  separately reviewed host integration must supply the concrete browser
  executor and non-forgeable completion attestation before any positive
  experiment decision. Do not migrate those controls into `capacity_c10`.

- **Freeze the validity strata before any C10 execution** (priority: highest;
  confidence: verified requirement)
  Treat the fixed 20-source matrix as a compatibility panel, not a pooled
  causal population. Predeclare a browser-sensitive causal subset only after
  each source has reviewed, source-native browser-relevant transport and
  fidelity evidence. Separately predeclare a direct-control stratum of sources
  with a reviewed direct native path. Estimate effects within the
  browser-sensitive subset, report the direct-control stratum as a trend and
  implementation control, and retain unclassified or blocked sources as
  compatibility-only. Do not infer membership from a plane label or from a
  receipt descriptor.

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
