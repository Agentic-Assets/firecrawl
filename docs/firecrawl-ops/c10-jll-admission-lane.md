# JLL-only C10 admission lane

## Scope and boundary

The existing 20-source compatibility panel remains an explicitly non-admitting
report.  Its blocked or unavailable descriptors never become executable by
virtue of this lane.  This lane is a separate, JLL-only experiment contract:
one fixed JLL GraphQL enumeration card and exactly sixteen JLL member cards.
It has its own cohort kind, plan kind, and checked-in authority file.  A
20-source authority cannot authorize this lane and a JLL authority cannot
authorize the panel.

## Canonical flow

1. An operator first uses the existing descriptor-relative owner-0700 root
   provisioner.  Receipt and admission roots are fresh, distinct, private
   leaves.  The command performs no database, cache, listing, scheduler, or
   status mutation.
2. `production.execute_jll_admission_collection` is the only provider-facing
   admission action. It starts a fresh loopback-only sidecar at capacity one,
   which is deliberately not a P0/P1 calibration, then starts a dedicated
   typed child bridge bounded to the existing source-owned JLL receipt producer:
   one reviewed sale/office/page-1 GraphQL card, then the selected sixteen
   canonical JLL member routes.  It exposes no URL, host, method, request body,
   adapter, or arbitrary transport option.  Its execute seam accepts only a
   controller-issued one-shot transport; it is deliberately unavailable to a
   caller without that controller. The child has neither a browser endpoint,
   private-root descriptor, signing key, nor generic fetch surface. It can
   frame only the pre-bound 1+16 source cards and private-seal requests; the
   controller verifies the card sequence, issues each C10 v3 capability,
   verifies the signed loopback evidence, and owns every write. Its recording store captures the complete
   sealed artifact index and seals the manifest through that same private root.
   Dry-run validates the fixed graph and roots but makes no provider request.
3. The source producer seals its private response/event/graph artifacts and
   emits public receipts.  The JLL manifest binds the full receipt set, exact
   card-set digest, selected provider IDs/routes, current adapter digest and
   no-write declaration.  Missing, malformed, challenged, wrong-route, stale,
   duplicate, partial, or tampered evidence fails closed. Python reopens every
   indexed owner-0600 artifact descriptor-relatively and rehashes it before
   accepting its public receipt commitment. A blocked response is not a JLL
   cohort.
4. `build-jll-cohort` reopens the sealed receipt manifest, verifies its digest
   and exact 1+16 receipt shape, and writes an immutable review bundle in the
   admission root.  `render-jll-authority` reopens the same artifacts before
   rendering a reviewable proposal.  It never installs or edits authority.
5. A human-reviewed change may pin the rendered proposal into the distinct
   repository JLL authority.  Only then can the existing production controller
   use the JLL plan for its P0/P1 arms.  The controller remains the only
   supported browser-work entry point; this admission tool cannot execute
   provider work directly.

## Threat model

Supported callers cannot select artifact roots, source keys, cards, or an
authority file to manufacture an executable record.  The private-root checks
defend descriptor-relative path replacement and accidental same-user misuse;
they do not claim to defeat a hostile process with the same OS uid that can
alter checked-out source or private files.  Such an adversary requires OS
account/container isolation, outside this repository's supported runtime
contract.

## Current state and forward queue

No provider request has been made by this branch. `collect-jll --execute`
continues to refuse: it is intentionally not a bypass around the controller.
After code review and merge,
the only permitted live next step is one controller-issued receipt collection
against the fixed sale/office/page-1 JLL graph and its selected 16 members.
It must use the production admission action, render rather than silently
install the separate JLL authority, and receive a reviewed pin before P0/P1
execution. Its own deadline and sidecar cleanup gates apply; it does not claim
an arm transition, settlement, or calibration result. No direct-fetch fallback
is permitted. The 20-source compatibility report stays non-admitting.
