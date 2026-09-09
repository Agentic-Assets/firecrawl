# Forward queue after MR-R09 CBRE ingest recovery

Candidate follow-up work surfaced during recovery. This is a menu, not a
roadmap.

## Hardening

- **Execute a full generated high-volume transaction in disposable PG17.6**
  (priority: high; confidence: verified gap). The current contract executes the
  failure-prone join and lifecycle concurrency, but not every statement in a
  complete CBRE transaction against a disposable production-shaped schema.
- **Make retained dry-run SQL preservation explicit** (priority: high;
  confidence: verified gap). Failed series logged a retained SQL path that was
  later empty, weakening byte-for-byte incident reproduction.

## Robustness

- **Preflight collector Node dependencies before source work** (priority:
  medium; confidence: verified gap). A fresh worktree reached collection before
  detecting missing lockfile dependencies; fail before creating a series.
- **Expose source retry reasons in the parent manifest** (priority: medium;
  confidence: verified gap). Strict source retries are visible in child logs but
  not clearly summarized in series-level state.

## Operations

- **Resume the all-source series during a quieter host window** (priority:
  high; confidence: verified need). Preserve the conservative resource profile
  and do not admit freshness until all required sources and live inventory
  fingerprints reconcile.
