# Forward queue after C10 production integration (2026-09-15)

Candidate work surfaced during this integration. This is a menu, not a
roadmap.

## Hardening

- **Complete authenticated evidence for all 20 sources** (priority: highest;
  confidence: verified gap)
  The production host currently authenticates the sealed JLL lane only. Add
  independently reviewed host paths for every admitted source before enabling
  the 20-source rate comparator.

- **Run TypeScript C10 receipts from a dependency-complete clean worktree**
  (priority: medium; confidence: verified environment gap)
  The isolated worktree lacked `tsx`, so its TypeScript tests did not begin.
  Re-run them without changing dependencies or source before relying on that
  lane as integration evidence.

## Evaluation

- **Exercise a human-approved P0 then P1 smoke** (priority: highest;
  confidence: deferred human gate)
  The production route is intentionally unexecuted. A future operator must
  supply fresh per-arm approvals and runtime receipts, then retain the sealed
  artifacts and rollback proof for review.
