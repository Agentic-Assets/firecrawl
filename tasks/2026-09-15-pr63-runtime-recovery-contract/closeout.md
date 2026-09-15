# PR #63 CRE runtime recovery contract closeout

**Branch:** `fix/cre-runtime-recovery-contract`
**Merged:** [PR #63](https://github.com/Agentic-Assets/firecrawl/pull/63) at
`fa8204c1d6d70f926555f2c81f6e64083ebc4dbd` on 2026-09-15
**Reviewed head:** `fddc5ce31f46f7598ac57f6cee90993606bfdc49`
**State:** merged code only; governed runtime recovery and the capacity
experiment remain intentionally unexecuted.

## Goal

Make CRE quarantine recovery, persistent lock authority, tier dispatch, and
resource admission fail closed and crash resumable without granting a runtime
mutation path outside the governed operator workflow.

## What shipped

- Extracted the quarantine archive and replay state machine plus its bounded,
  append-only guard journal into dedicated modules. The runtime CLI remains a
  thin owner of observation callbacks and policy.
- Bound recovery, normal acquisition, tier dispatch, and legacy repair paths to
  the same persistent authority and recovery synchronization protocol. Tier
  workers inherit proven flocked descriptors and use a dedicated process group
  for signal handling.
- Added durable, no-clobber forensic archiving and replay phases with
  identity, mode, ownership, receipt, and namespace validation. Completed
  guards admit subsequent ordinary lock cycles while dangling lock or authority
  symlinks fail before any claim or write.
- Centralized loopback topology and the governed baseline resources, including
  API `3002`, browser `3003`, API 8 GiB, browser 16 GiB, and browser shared
  memory 8 GiB.
- Added the repository-local thermo-nuclear code-quality-review skill from
  `cursor/plugins` commit `c1c0a32802223f4be824112dd83d33ad29a8b26c`.
  Its `skills-lock.json` entry uses the supported immutable `ref` plus the
  verified folder-content hash.

## Verification

- Final full collector Python suite on `fddc5ce`: 3,112 passed, 1 skipped.
- Final focused recovery/checkpoint/dispatcher suite: 469 passed.
- Collector `npm test`: TypeScript typecheck plus 864 passing tests.
- Post-merge offline smoke on clean `main`: checkpoint/runtime/benchmark 611
  passed, 1 skipped; collector typecheck plus 864 passing tests; `git diff
  --check` passed.
- Changed Python lint, formatting, and compilation checks passed. The final
  thermo-nuclear review and the fresh Codex exact-head review reported no
  actionable finding. The final review thread and inline audits were empty.

## Decisions

- Recovery journals are persistent, FD-bound evidence, not a pathname that can
  be replaced or unlinked during recovery. A malformed, substituted, or torn
  journal stops for operator review.
- The protocol guarantees cooperating-process serialization. A direct,
  same-UID filesystem mutation remains outside that cooperative boundary; every
  destructive phase revalidates and preserves evidence rather than selecting a
  competing namespace.
- Runtime recovery was not exercised during this coding and merge workflow.
  The stale local quarantine residue remains an operator-controlled incident,
  not test-cleanup authority.

## Rollback and operator boundary

Revert merge commit `fa8204c1d6d70f926555f2c81f6e64083ebc4dbd` through a
reviewed branch if code rollback is required. Do not manually remove lock,
authority, guard, quarantine, or forensic artifacts. Any local runtime
recovery, baseline restoration, admission, or experiment needs its own fresh
governed preflight and explicit operator action.
