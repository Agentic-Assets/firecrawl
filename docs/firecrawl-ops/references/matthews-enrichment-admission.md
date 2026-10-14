Parent: [AGENTIC-4200](https://linear.app/agenticassets/issue/AGENTIC-4200); protective admission correction: [AGENTIC-4201](https://linear.app/agenticassets/issue/AGENTIC-4201).

# Matthews targeted enrichment admission

Plain Matthews `mode=enrich` output is refused before ingestion. Both the
enrichment worker and direct `cre_ingest.py --in` entrypoint use the existing
strict artifact freshness validator. The CLI also refuses unsafe dry-run SQL
generation. This prevents a successful partial detail record from replacing
existing `raw_data` while discarding inventory and generation proof.

The check applies to emitted Matthews records. A valid empty/all-omitted batch
keeps the existing retry/dead-letter accounting without ingestion. A rejected
artifact follows the existing whole-run failure path: release claims without
incrementing attempts or falsely completing them. A mixed batch containing
unsafe Matthews detail is rejected as a whole. Other sources and full-mode
Matthews validation retain their existing behavior.

An emitted Matthews enrichment record must satisfy the existing strict
generation envelope and observation-clock checks, declare `detail_page`, and
remain canonical live detail with its actual `detailObservedAt`. Cached
validation time cannot substitute for that observation.
Inventory-only/feed and child-preservation shortcuts do not admit a targeted
Matthews result. Preservation checks cover recursive JSON boolean `true`,
string `"true"` and array-wrapped markers, matching the ingest SQL; unrelated
scalar values keep their existing behavior. The checks do not synthesize clocks,
rewrite metadata, issue a generation, or turn partial enrichment into a full
source snapshot. `strict_full_source_scope` continues to reject mode=enrich.

The current ordinary `collect.ts --enrich-input` envelope has no strict
generation/inventory assertion. Setting a queue timestamp or artifact finish
time as `inventoryObservedAt`, or carrying an older full generation onto newly
changed detail, does not repair this contract. A new real sitemap response may
establish an inventory observation, but a separately reviewed targeted-refresh
contract must bind actual inventory membership, exact native identity/tenure,
actual detail time and honest partial-generation receipts before queue writes.

AGENTIC-4200's source-owned authoritative adapter remains a private prototype;
this correction does not register it or enable production targeted writes.
Source-specific no-write detail diagnostics need their own reviewed code,
selection, resource/ownership admission and bounded capture. Queue dry-run
fetches no detail. Queue apply claims and writes data; do not use it as a
diagnostic workaround. No baseline, lifecycle/status, OM, scheduler or full-source
coverage authority is granted here.

See [Windows onboarding](windows-docker-desktop-onboarding.md) for the host
admission and setup boundaries. Existing Mac full-refresh and other-source
enrichment workflows retain their contracts.
