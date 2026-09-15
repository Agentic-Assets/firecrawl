# Forward queue after PR #63 CRE runtime recovery contract

Candidate follow-up work surfaced during the PR #63 recovery hardening. This
is a menu, not a roadmap. Verify each item before acting.

## Operations

- **Run the governed operator recovery preflight** (priority: high;
  confidence: verified pending work). The local stale test residue and
  quarantine evidence were deliberately left untouched. An operator must use
  the documented dry-run and explicit execution path only after fresh idle,
  ownership, resource, and baseline-contract evidence.
- **Re-admit the six-arm capacity experiment only after recovery** (priority:
  high; confidence: verified pending work). It requires new approval and
  admission evidence for every arm, settlement proof, and the documented stop
  conditions. This merge is not experimental performance evidence.

## Hardening

- **Add a CI job for the lock/recovery fault matrix** (priority: medium;
  confidence: verified gap). The critical crash-prefix, descriptor-inheritance,
  no-clobber, and completed-guard tests run locally but are not represented by
  a visible PR status check.
- **Add automated skills-lock restore validation** (priority: medium;
  confidence: verified gap). The lock now uses the supported immutable Git
  `ref` and folder-content hash, but CI does not yet exercise a clean restore
  and hash comparison for every pinned project skill.

## Simplification

- **Revisit the large recovery state-machine test matrix after runtime
  acceptance** (priority: low; confidence: passing idea). The dedicated suite
  is cohesive and reviewed, but stable production evidence may identify common
  fixtures that can be consolidated without reducing crash-prefix coverage.

## Evaluation

- **Capture an operator-reviewed recovery receipt as an operational artifact**
  (priority: high; confidence: verified need). It should document only the
  governed command result, exact artifacts, idle/resource evidence, and whether
  a rollback lock could be reacquired. Do not treat it as permission for a full
  refresh.
