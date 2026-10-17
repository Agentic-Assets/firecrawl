Parent: [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# CRE Brokerage Completion Playbook

Last reviewed: 2026-10-16.

Use this playbook when upgrading one brokerage website from shallow coverage to
complete public-feed coverage for EQUIRE. It is adapter-development guidance,
not production refresh authorization. Work one brokerage at a time.

## Completion Standard

A brokerage is complete only when all of these are true:

1. Discovery covers the public source feed with real pagination, not just the first rendered batch.
2. Sale and lease modes are both understood, including sale-only or lease-empty platforms.
3. Each listing has a stable external id or a documented fallback id.
4. Each listing captures the best public structured fields available: title, transaction type, property type, address, city, state, ZIP, coordinates, price/rate text, size, status, description, brokers, and source URL.
5. Detail pages are scraped when the search feed does not expose enough data.
6. Document and image assets are captured as URLs only. Do not download or upload PDF/image binaries into Supabase storage.
7. Contacts include visible names, phones, emails when public, profile URLs, avatar URLs, and VCard or contact-card URLs when available.
8. A target listing is tested against manual expectations for documents, images, contacts, and key facts.
9. The collector passes typecheck, the ingestor dry-run stages expected rows, and docs record the remaining limits.

## Comprehensive listing fidelity

Collect all useful listing information the permitted public source provides.
Normalize fields supported by the existing vocabulary and preserve additional
relevant source information for later use. This is the collection standard,
not a claim that every adapter or consumer already meets it. Row counts,
successful requests and a populated UI do not establish semantic completeness.
Reported JLL or Marcus UI snapshots are investigation leads, not verified
source facts or evidence that a particular field is missing from the source.

Review descriptions and highlights; spaces, suites and availability; sale/lease
tenure; asking prices, rent/rate terms, expenses and financial facts; building,
land and available areas with units; unit counts; amenities; location/geo;
public broker/contact details; and all relevant image, media and document URLs.
Preserve original values, text, units, currency, rate period/basis and source
provenance alongside supported normalization. Never invent values, infer an
unknown tenure from a missing price, or turn missing values into zero. A sale
price can be inapplicable to a lease-only offering; an explicit public
price-withheld control must remain withheld through every downstream channel.
Keep offered listing tenure and asking rent separate from in-place tenant lease
facts; tenant rent or lease type does not establish the offered transaction.
Retention follows source visibility/redaction and shared ownership rules; use
structured provenance rather than promising complete raw HTML/header retention.

Trace a representative fresh detail record through each existing boundary:

1. Permitted public detail/feed payload: identify the source field, value,
   unit, URL/record identity and observation time; include relevant variation
   such as sale, lease, dual tenure, spaces and withheld/unknown values.
2. Adapter and saved artifact: verify the information survives mapping with
   original source metadata. Preserve useful long-tail facts through existing
   `extraFacts` and source-specific metadata rather than discarding them merely
   because no scalar column exists. Preserve source identity recipes.
3. `cre_ingest.py` mapping/merge: check supported normalized columns,
   `markdown`, `extraFacts` → `extra_facts`, and retained source data in
   `raw_data`; inspect the actual mappings rather than assuming arbitrary
   metadata is automatically normalized. Verify sale/lease merging retains
   distinct terms and assets without inventing or narrowing tenure.
4. Saved database readback: check parent values and the existing contacts,
   images, media, links and documents children against the same artifact.
   Respect existing lifecycle, child-preservation and guarded-write contracts.
5. Consumer query/view and display: verify which stored fields it selects and
   renders. An empty UI field proves neither an absent source value nor an
   extraction failure; a value retained only in `raw_data` can still be unmapped
   or undisplayed.

Use these as evidence/review categories, not new implemented database fields:

| Category | Evidence to retain |
|---|---|
| Absent | An inspected applicable source field is explicitly empty or missing; bound this conclusion to that payload/observation. |
| Not applicable | Source-backed offering/property context makes the field inapplicable, such as sale price for a lease-only offering. |
| Not exposed | The examined public surface does not expose the value; availability elsewhere remains unknown. |
| Extraction failed | Acquisition or parsing failed; record the failure rather than claiming source absence. |
| Not mapped | The captured source/artifact value has no verified normalized destination yet. |
| Not displayed | The stored value exists but the consumer does not select/render it. |

Use fresh representative saved detail payloads and semantic assertions across
these boundaries, including optional rich fields and failure cases. Verify
value meaning, units, text and asset roles, not just non-null or child counts.
Do not reduce detail coverage, truncate descriptions/galleries or discard rich
fields to meet a speed target; compare performance with fidelity held constant.
Document unresolved mapping/display gaps on the existing parent or scoped issue.

Capture document/media URLs through the existing harvester and child contracts;
do not count a thumbnail, preview image or unrelated listing asset as a document
or additional evidence of richness. Deduplicate by evidenced asset identity
without collapsing distinct documents, units or offerings. Keep original URL
and role provenance where supported. The [dated media capture handoff](../../../scripts/firecrawl-ops/cre_collector/HANDOFF_MEDIA_CAPTURE_2026-06-15.md)
records the existing mechanisms and historical implementation evidence.

When reconciling historical media rows, separate malformed URL representations
and UI contamination (such as consent/map/placeholder imagery or an explicitly
cross-property recommendation) from assets owned by the listing. For apparent
URL losses, pair the exact-generation historical and current native roles, then
check every staged channel: a changed URL may be a publication substitution or
the asset may remain under a typed floorplan/document role. This proves only
reference continuity; do not infer a global CDN basename alias or identical
remote bytes. Raw child counts alone prove neither richness nor asset loss.
Malformed-reference cleanup must preserve valid and ambiguous references, use
the existing repair approval and locking gates, and leave quality guards intact.
The [JLL repair preparation](https://linear.app/agenticassets/issue/AGENTIC-4371)
records a pending case, not approval to delete production rows.

Use permitted public access and provider-aware pacing. Preserve existing
deployment and guarded-write procedures when adding fields. Retaining a
document URL does not prove its contents were downloaded or parsed. GetCREdata
remains the sole production OM extraction writer under the shared CRE ownership
contract; do not add a collector PDF/OM extraction writer or OM-facts writes.

## Investigation Workflow

1. Start with the source folder:

```bash
# From the repository root on macOS, Linux, or Ubuntu/WSL:
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
```

2. Inspect the public website with browser devtools or the Browser plugin:

- Search for XHR and fetch requests while loading the listing page.
- Change filters, pagination, map/list mode, and text search.
- Record GET endpoints first. Treat POST-only or consent-gated paths as blocked until a repeatable safe path is found.
- Save endpoint shape, required query params, page size, and total-count fields.

Published keyword, geography and property-type filters do not by themselves
prove exhaustive or disjoint inventory partitions. Test matched known
multi-state, portfolio and hierarchical-type members, then reconcile the full
native identity union before using those filters for sharding. A filtered
omission alone does not establish the cause of a count gap or offering status;
see [AGENTIC-4297](https://linear.app/agenticassets/issue/AGENTIC-4297).

3. Test endpoints through local Firecrawl:

```bash
python3 scripts/firecrawl-ops/firecrawl_request.py scrape '<api-url>' \
  --formats rawHtml --out /tmp/source-api.json
```

Use local Firecrawl even when direct Python or curl gets blocked. Some sites
serve the local browser path but reject plain urllib.

4. Build the collector adapter:

- Prefer public JSON APIs over rendered cards.
- Prefer first-party structured records and semantic fields over selectors tied
  to card position or visual order. Use DOM or rendered-page fallbacks only when
  primary evidence shows the required public field is unavailable elsewhere.
- Use full pagination and cap by `--max-items`.
- Preserve source raw rows in `raw_data`.
- Add bounded concurrency and explicit per-listing failure evidence.
- Exploratory probes may retain a feed row with `detailError`. Strict admission
  fails closed on incomplete required detail. Only an explicitly registered
  authoritative-inventory or scoped contract may preserve existing children.
- Preserve raw provider IDs and original URLs as captured in provenance, and
  keep each source's documented canonical external-ID recipe (for example, a
  prefix, hash, path key, or proven provider suffix rule). Apply source-contract
  comparison normalization consistently in the mapper and verifier; case,
  whitespace, or optional `www` equivalence is allowed only where that contract
  says so. Do not change an identity recipe as a harmless normalization.

5. Detail-page enrichment:

- Scrape `rawHtml`, `markdown`, and `links`.
- Parse JSON-LD when present.
- Scan raw HTML for asset URLs. Do not rely only on Firecrawl's extracted links.
- Dedupe image variants and prefer high-width URLs when the CDN exposes width params.
- Exclude related-listing photos by using page-specific media IDs, document media IDs, or the first listing media group.

6. Targeted proof:

- Pick one known listing with manually visible documents, photos, contacts, and facts.
- Add a temporary or environment-gated query hook when the source supports text search.
- Save a small targeted artifact under `/tmp`.
- Confirm counts for document URLs, image URLs, contact URLs, and key facts.

7. Ingest proof:

```bash
python3 cre_ingest.py --in /tmp/source_target_probe.json \
  --dry-run --keep-artifacts /tmp/source_target_ingest_check
```

Check that staged child rows contain URLs only and no binary payload fields.

8. Development artifact proof (not CPU-guarded and never a production refresh):

```bash
npx tsx collect.ts --source=<source-key> --transaction=both \
  --max-items=0 --page-cap=400 --concurrency=2 \
  --out=out/<source>_full_<timestamp>.json
python3 cre_ingest.py --in out/<source>_full_<timestamp>.json --dry-run
```

Do not treat the direct artifact/dry-run pair as production proof or use it for
a full registry. A supervised production refresh uses
`cre_checkpoint_refresh.py` for a selected source or `cre_checkpoint_series.py`
for the registry, with the CPU resource profile in `START_HERE.md`. Those paths
bind a clean pushed SHA, freshness generation, source and aggregate gates,
dry-run, additive ingest, and exact database readback. Lifecycle reconciliation
is separate and requires the exact source gate to report `mark_missing_safe`,
explicit founder approval for the exact scope, and the applicable operator
readback. Adapter completion does not grant lifecycle authority.

9. Monitor enablement (when enumeration key matches ingest `external_id`):

```bash
npx tsx collect.ts --source=<source-key> --transaction=both --monitor \
  --max-items=0 --out=/tmp/<source>_monitor.json
python3 cre_monitor.py --in /tmp/<source>_monitor.json
```

Sources whose persisted id is detail-derived (`jll`, `jll-investor`,
`cbre-dealflow`, `colliers` SalesTracker) must stay on full-sweep cadence and
return zero monitor rows. Never feed a monitor artifact to `cre_ingest.py`. See
`cre-monitor-subsystem.md` and `sources/CLAUDE.md` (monitor matrix).

## Website variation and evidence policy

These are expectations for new or changed adapter work, not a claim that every
existing adapter already meets them or that a pending fix has shipped.

Treat a changed page as a question to classify, not as permission to relax the
adapter. Separate presentation or transport changes from a provider/feed
change, host-coordinate drift, identity mismatch, parser vocabulary gap, and a
coverage or baseline-policy hold. A similar-looking site, hostname, address, or
brokerage name does not establish that two records or endpoints are the same.

Use first-party provider documentation, the firm's current official site, and
the exact registered source configuration as primary evidence. Reproduce a
suspected failure with the current adapter's URL, query, headers, referer,
redirect policy, and parser before changing behavior. Keep any diagnostic
request finite and retain its timestamp, status, request metadata, response
hash, and sanitized fixture privately. A successful page proves only that page;
prove pagination, declared totals, unique native IDs, and source ownership for
the complete feed separately. Do not treat an ad-hoc client's response as proof
of a production adapter failure.

Map fields by meaning, not by DOM position. Use provider field names and
semantic labels where available; do not infer a field from its visual order or
silently substitute a default for an essential source-contract fact, such as
transaction type, identity, or ownership (and address where the source requires
it). Add field aliases or layout fallbacks
only after primary evidence establishes their meaning and regression tests cover
the observed variation and its negative cases. For example, a Sublease token
that the current parser does not recognize is a parser-vocabulary defect; that
observation alone does not show the provider changed its site.

Validate the property facts together rather than requiring an incidental
heading arrangement. A source-evidenced street abbreviation or a complete
address combined into one heading can be equivalent to separately labeled
fields. Require the same full property identity and owned page bindings, keep
raw values unchanged, and test a different street number, missing visible
address, conflicting metadata and ambiguous identity containers. A matching
city or marketing headline alone must never substitute for the street witness.

Use one source-owned normalization rule wherever the mapper and verifier
compare the same value. This comparison rule does not replace or alter the
source's documented canonical external-ID recipe. Keep raw native IDs and
original provider URLs byte-for-byte in provenance. Do not infer aliases or
migrate identities from URL similarity, names, addresses, coordinates, or a
new hostname. A changed coordinate requires current first-party ownership
evidence tied to the exact registered source configuration; a provider switch
requires evidence of the new feed and a reviewed identity strategy.

Distinguish a publisher's post, an observation of that post, the physical
property and the particular offering. A shared property ID or URL does not
prove identical offerings or supersession. Test conflicting price/currency,
sale/lease terms, contacts and assets before proposing a consolidation, and
inspect actual parent-overwrite and child-refresh behavior before claiming
that storing `raw_data` or `extraFacts` preserves every publication's history.
NAI's unresolved association contract is tracked in
[AGENTIC-4284](https://linear.app/agenticassets/issue/AGENTIC-4284).

If a source-policy-required value is unknown, required pages are missing, or
two records claim an incompatible identity, fail the affected source's
required-member proof and preserve the last-known-good data. A source artifact
cannot qualify as complete while any required member is missing or failed.
Diagnostics should identify the source key, stage, native provider ID when
available, field, observed value or shape, expected schema/rule, and receipt or
fixture reference without exposing credentials or unrestricted response
bodies. Continue after a source-local failure only through a runner that
explicitly supports that behavior. Global ownership, resource, evidence, or
uncertain-write failures stop the run; do not retry an uncertain write. Retry
only bounded, classified transient failures, not a schema mismatch or an
unexplained completeness failure.

Build regression fixtures from sanitized, genuinely observed provider responses.
Test the current layout, each supported variation, and mutations for missing,
conflicting, malformed, or newly unknown critical fields. Keep fixtures and
tests on the real production mapper/verifier path rather than duplicating its
logic. Record meaningful observations, rejected hypotheses, and experiment
results in the existing [AGENTIC-2895 learning log and scorecard](https://linear.app/agenticassets/issue/AGENTIC-2895)
and its existing experiment children; do not create a run log or new tracking
framework. Keep macOS/Linux collection and validation semantics shared; a
Windows wrapper may forward supported settings but must not create a separate
provider interpretation.

## Documentation Required Per Brokerage

For an adapter change, update the active source contract and its focused tests
in `scripts/firecrawl-ops/cre_collector/sources/` and
`scripts/firecrawl-ops/cre_collector/tests/`. Record durable decisions,
meaningful evidence, and experiment results in the existing AGENTIC-2895 issue
or its existing children. Keep raw provider responses, credentials, and
one-session working files out of tracked documentation; use ignored `.scratch/`
for private evidence. Do not create a legacy scraper README, a dated local
handoff, or a local backlog solely to document a source run. Durable guides
should describe the adapter contract and remaining limits, not assert live
counts without fresh readback.

## Cushman Pattern To Reuse

Cushman & Wakefield's historical adapter investigation established the API and
detail patterns below:

- Finding the public API in browser network traffic.
- Verifying local Firecrawl could fetch the API even when direct urllib got `403`.
- Replacing rendered-card scraping with full API pagination.
- Scraping detail pages for facts, JSON-LD, contacts, PDFs, and images.
- Scanning raw HTML for PDF URLs because Firecrawl's extracted links omitted them.
- Saving document/image URLs only.
- Proving the known 1800 Central listing captured 2 PDFs, 15 photos, and the broker contact links.

The current strict Cushman path uses its uncached, exactly reconciled public
search API as an authoritative inventory feed and intentionally preserves
existing children. It proves inventory freshness, not a new rendered-detail or
fresh-child sweep. Reuse the investigation techniques above, but follow each
source's registered admission and child contract.

## Sitemap-Discovery Pattern To Reuse (Cloudflare-protected sites)

Colliers main (`colliers-main`, 2026-06-13) was unblocked when neither a public
search GET nor a usable Coveo POST existed, by discovering listings through the
site's own XML sitemap. Reuse this when a brokerage hides search behind
Cloudflare/JS but still publishes a sitemap:

1. Fetch `robots.txt` through local Firecrawl (not direct GET, which Cloudflare
   403s). Read the declared `Sitemap:` URL and confirm `User-agent: *` allows
   crawling. Try the bare `/sitemap` path, not only `sitemap.xml`.
2. Fetch the sitemap through local Firecrawl. Walk the `<sitemapindex>` to the
   relevant child (e.g. `?type=properties`), then parse the `<urlset>` for
   detail `<loc>` URLs plus `<lastmod>` (refresh semantics). Generic helpers
   `extractSitemapLocs()` in `sources/colliers-main.ts` and shared
   `extractSitemapUrlEntries()` in `lib/html.ts` do this.
3. Render each detail URL through local Firecrawl with `proxy: stealth` and a
   `waitFor`. Parse `RealEstateListing` JSON-LD for the reliable fields
   (transaction in `name`, category, canonical URL, primary image) and the
   markdown/HTML for the rest (price, size, coordinates, photos, named PDF docs,
   broker contacts).
4. Handle Cloudflare rate-limiting: under sustained paging the site returns 429
   "Just a moment..." challenge shells. Detect them and retry with backoff; the
   stealth proxy rotates IPs per request, so a retry usually lands clean
   (`scrapeColliersMainDetailDoc`).
5. Tombstone non-listings so they are cached, not re-fetched, and never
   ingested: 404 / "Property Not Found" (`skip:"not_found"`) and real 200 pages
   on an alternate template with no JSON-LD (`skip:"no_structured_data"`).
6. Cache enriched rows in a durable JSONL file so a multi-hour run resumes
   across attempts; memoize across the sale and lease passes and partition by
   transaction client-side.
7. Fold into the parent brokerage with a prefixed external_id (`main:<id>`) in
   `SOURCE_TO_BROKERAGE`, and add a matching `external_id LIKE` branch to
   `cre_validate.py`'s source-key CASE so the report attributes folded rows.

Document/image URLs only; no POST replay, auth, or gated documents.
