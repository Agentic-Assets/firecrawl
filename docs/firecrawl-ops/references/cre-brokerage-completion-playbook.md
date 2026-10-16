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

Use one source-owned normalization rule wherever the mapper and verifier
compare the same value. This comparison rule does not replace or alter the
source's documented canonical external-ID recipe. Keep raw native IDs and
original provider URLs byte-for-byte in provenance. Do not infer aliases or
migrate identities from URL similarity, names, addresses, coordinates, or a
new hostname. A changed coordinate requires current first-party ownership
evidence tied to the exact registered source configuration; a provider switch
requires evidence of the new feed and a reviewed identity strategy.

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
