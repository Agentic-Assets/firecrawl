Parent: [AGENTIC-4206](https://linear.app/agenticassets/issue/AGENTIC-4206); authoritative adapter: [AGENTIC-4200](https://linear.app/agenticassets/issue/AGENTIC-4200); protective admission correction: [AGENTIC-4201](https://linear.app/agenticassets/issue/AGENTIC-4201).

# Matthews targeted enrichment admission

AGENTIC-4206 owns the caller-bound targeted contract below. Code verification,
an admitted production trial and recurring scheduling are separate requirements.
The issue records current implementation, verification and production admission
evidence. This reference does not certify a complete 51-source refresh or an
enabled schedule.

## Targeted writer acceptance

The new path is restricted to the existing one-shot Linux/WSL guarded runner.
It must validate the actual owner and worker process identities, canonical held
lock and current run receipt before database access. A direct worker flag,
environment variable or copied claim file cannot grant that ownership.
The guard owns one inherited worker/collector/database process group and a work
deadline of at most 600 seconds; the batch is at most 50 claims and detail
concurrency is 1. Existing Mac ordinary and full workflows retain their
contracts; this new applied lane requires its supported Linux/WSL owner.

The worker allocates a new batch generation before provider calls. Its private
selection, environment and returned partial artifact must agree on the exact
source, generation and start. The source obtains a new valid sitemap response,
checks exact native membership and derives genuine sale/lease tenure before
fetching details. Inventory and detail timestamps describe separate completed
observations. A valid sitemap with an individually missing or ambiguous target
omits that target; exact still-owned retry/dead-letter processing can advance
without blocking valid peers. An invalid batch context or sitemap refuses the
batch. Neither case creates a full-source freshness certificate.

The write transaction must lock and recheck every emitted target: exact queue
claim identity and original `claimed_at`, an existing active/nondeleted listing,
and the matching present/nonretired source-index identity and URL. It refuses
newer stored observations and never inserts a new identity or changes lifecycle
or presence state. Inventory clocks update from inventory observation, not
detail time. The existing stored `sale_or_lease` merge behavior is preserved;
the raw provider observation remains exactly the newly observed sale or lease.

Readback must match the new batch, native identity, source/canonical URL,
provider tenure and both observation clocks. Claim completion rechecks that
evidence and current eligibility inside its own transaction. A changed,
retired, reclaimed or uncertain target keeps the claim and recovery evidence;
a successful process exit alone cannot authorize completion.

Only after the issue records verified code deployment and concrete trial
admission, use the existing Windows entrypoint for a small applied batch:

```powershell
pwsh -NoProfile -File scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1 `
  -Tier enrich -Source matthews -Batch 2 -ExpectedSha <full-reviewed-sha> `
  -DeadlineSeconds 600 -Apply
```

This command claims and updates real database records. Obtain current target,
sole-writer, resource and eligibility evidence first. It neither installs a
schedule nor seeds coverage baselines, changes status, retires listings or
writes OM extraction facts. Without `-Apply`, enrichment prints proposed claim
SQL and performs no provider or database work.

## Ordinary enrichment refusal

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

Ordinary, unbound adapter input must supply an accurate sale/lease tenure that
agrees with the requested property URL. The ordinary Python queue claim SQL has no transaction
field, and `claim_rows_to_items` defaults ordinary claims to `sale`. A queued
Matthews lease URL therefore fails the adapter's tenure check before transport
and remains omitted; explicit accurate lease input works for no-write detail
diagnostics. Queued lease enrichment is not enabled by this registration.
The caller-bound path above instead omits unknown queue tenure and derives it
from the new sitemap response. This retains native URL/provider agreement
without guessing from the legacy default. Its acceptance remains separate
from the source adapter registration.

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
