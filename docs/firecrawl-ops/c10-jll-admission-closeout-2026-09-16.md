# C10 JLL admission lane progress and forward queue

## Verified repository state

- PR #67 is merged production-host hardening. It is not evidence of a live
  provider run.
- PR #70 is merged offline reviewed-admission-chain support. Its 20-source
  compatibility panel remains non-admitting: blocked or unavailable rows are
  never executable C10 evidence.
- PR #71 is merged JLL-only admission-lane support. It introduces a separate
  JLL authority, cohort, plan, source-card intent and sealed receipt artifact
  index. It does not change the 20-source authority.

## What this branch proved locally

The fixed JLL receipt producer accepts only the reviewed sale/office/page-1
GraphQL request plus sixteen canonical JLL members through a controller-owned,
source-bound one-shot transport. Its manifest records every sealed artifact.
Before bundle or authority rendering, Python reopens every indexed owner-0600
artifact from a retained owner-0700 directory descriptor and checks byte count
and SHA-256. The collection intent commits the source POST-body hash and exact
member-route order, and the rendered plan binds that intent and receipt binding.

## Discovered controller bridge and unfinished external gates

The post-merge preflight found that the offline lane had no production-owned
TypeScript execution bridge. This branch supplies that missing boundary as a
typed framed child: the controller alone issues each signed C10 capability,
executes the loopback request, verifies evidence, and seals descriptor-relative
artifacts. The child can only request the fixed enumeration followed by the
sealed sixteen routes. It cannot select a root, call a provider, or create a
receipt outside the active production-controller authority.
The source, not the controller, deterministically selects the 16 members from
the signed enumeration using the versioned canonical-URL ordering rule; Python
recomputes and verifies the emitted selection digest before it can build a bundle.

No provider request, authority pin, P0/P1 arm, database/cache/listing write,
or live C10 result exists. After this bridge is reviewed and merged, the
required operator sequence is: provision fresh private roots; invoke
`production.execute_jll_admission_collection` through the reviewed operator
surface (not the refusing offline CLI);
review the rendered separate authority proposal; make a reviewed repository
pin; then run the existing production-controller dry-run before any bounded P0
arm. Preserve the receipt manifest and final outcome as operator evidence. Do
not treat this document or a passing mock test as live provider proof.
