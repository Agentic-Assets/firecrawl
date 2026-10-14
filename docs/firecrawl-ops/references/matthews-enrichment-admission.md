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

The Matthews enrichment resolver uses the source-owned `enrichMatthewsListing`
adapter. It reuses the direct HTTP transport, shared request-start pacing,
retry policy and separate 30-second header/body bounds. It validates an HTTPS
Matthews property URL, exact queued native ID, sale/lease tenure and the page's
canonical identity before returning the existing DOM parser's fields. It
retains the original request URL for completion matching, refuses redirect
aliases and invalid/error/not-found pages, and never falls back to generic
Firecrawl/JSON-LD after a source failure. Percent-escape spelling and trailing
slashes use the existing source identity normalization; arbitrary ID prefixes
and slug case changes are not stripped.

Targeted input must supply an accurate sale/lease tenure that agrees with the
requested property URL. The current Python queue claim SQL has no transaction
field, and `claim_rows_to_items` defaults ordinary claims to `sale`. A queued
Matthews lease URL therefore fails the adapter's tenure check before transport
and remains omitted; explicit accurate lease input works for no-write detail
diagnostics. Queued lease enrichment is not enabled by this registration.
Future truthful targeted-write admission must also own the tenure plumbing;
do not weaken native URL/provider agreement or guess from stale input.

Successful detail output records the actual completed `detailObservedAt` and a
validated canonical URL. It has no invented inventory observation or generation
proof. Ordinary `collect.ts --enrich-input` therefore produces an authoritative
partial detail artifact that the worker and direct ingest CLI still refuse.
Registering the adapter does not enable production queue writes or establish
full-source completeness/freshness. The direct source path uses no Firecrawl
scrape, browser render or AI model call.

A no-write diagnostic can reuse this same source adapter with an explicit
native ID, current source URL and tenure after reviewed selection and normal
resource/ownership admission. Keep the output private and bounded; it is not
an ingestible fresh-generation receipt. No new runner is needed. Queue dry-run
fetches no detail. Queue apply claims and writes data; do not use it as a
diagnostic workaround. No baseline, lifecycle/status, OM, scheduler or full-source
coverage authority is granted here.

See [Windows onboarding](windows-docker-desktop-onboarding.md) for the host
admission and setup boundaries. Existing Mac full-refresh and other-source
enrichment workflows retain their contracts.
