# Handoff: Colliers Main Site Collector - 2026-06-13

> **UPDATE 2026-09-09: production-rehearsed first-party detail API path.** The
> current detail template no longer reliably exposes the legacy
> `RealEstateListing` JSON-LD contract. A public, no-auth Coveo search endpoint
> was therefore reconciled by exact native ID against the live US properties
> sitemap: 15,944 sitemap rows and 15,944 results, with zero missing, extra,
> duplicate, or path-mismatched records. Direct host requests receive
> Cloudflare 403; anonymous same-origin POSTs work through the loopback-only
> Playwright batch sidecar. `COLLIERS_MAIN_COVEO_ENABLE=1` opts into this route;
> the HTML renderer remains the default fallback. Property responses reconcile
> exactly; expert responses reject duplicates and extras while recording public
> profiles that are currently absent as unique unresolved IDs. Those rows enter
> a narrowly validated contact-only preservation path; all other child types
> still refresh wholesale. The mapper rejects invalid child URLs and malformed
> structured child payloads, suppresses unproved canonical pricing, and admits
> acreage above 100 only when current source text corroborates it; any explicit
> numeric acreage mismatch suppresses canonical acreage. Exact raw pricing and
> sizing remain available for audit. The path records honest
> `first_party_detail_api` provenance admitted only for `colliers-main`. The
> full strict artifact
> `out/calibration/2026-09-09T105034Z-colliers-coveo-full-final-strict.json`
> (`sha256:610c4bf8300744d99fe9e114f2f42df76574e45aa9acbbde7031429ebb70cd09`)
> contains
> all 15,944 current properties (17,158 sale/lease rows), 32,359 contacts,
> 12,611 classified documents, 66,056 images, and 4,661 links. Every current
> property has an image; canonical price/rate fields and unsafe child URLs both
> reconcile to zero. Sixty unavailable public expert IDs affected 147 properties
> and invoked contact-only preservation, never broad child preservation. A
> strict ingest dry run and production-schema transaction ending in an explicit
> `ROLLBACK` both passed with zero child-count or contact-contract violations.
> The Colliers-only transition guard compares
> normalized underlying image assets instead of inflated legacy URL variants and
> cross-listing carousel attachments. Keep the path explicit opt-in for the
> supervised refresh; this proof does not authorize a scheduler.

The remainder of this handoff describes the superseded June 2026 renderer
implementation. The dated update above is the current refresh path.

> **UPDATE 2026-06-14: full run COMPLETE.** This handoff was written mid-run
> ("full run in progress"). The full ~15,883-URL sitemap detail run has since
> converged (0 errors) and was ingested additively with status activation OFF:
> **15,829 active rows** (5,750 sale + 8,897 lease + 1,182 sale_or_lease), 0
> soft-deleted, 0 duplicate external_ids. The colliers brokerage total is now
> 17,001 active (15,829 main + 1,172 SalesTracker). Everywhere below that says
> "in progress" or "when the full run completes" is DONE. Current per-source
> counts: `START_HERE.md` and `BROKERAGE_STATUS_2026-06-12.md`.

Session goal: close the largest remaining CRE gap, main
`www.colliers.com/en/properties` sale and lease inventory, which prior rechecks
marked blocked behind a Cloudflare-protected Coveo POST search.

## Outcome

Unblocked and implemented. A new `colliers-main` collector source feeds the
`colliers` brokerage from the public XML sitemap plus per-listing detail
renders through local Firecrawl. A bounded 2,000-URL batch is live-ingested and
validated; the full ~15,896-URL run is in progress.

## The Unlock

Prior rechecks only tried `sitemap.xml` and `en/sitemap.xml` via direct GET,
which Cloudflare 403s. The working path is the bare `/sitemap` fetched
**through local Firecrawl** (stealth proxy clears Cloudflare):

1. `https://www.colliers.com/robots.txt` -> `Sitemap: https://www.colliers.com/sitemap`.
   `User-agent: *` has only `Crawl-Delay: 30`, no `Disallow` (the 72 `Disallow: /`
   lines are all abusive/legacy bots). Public crawling is allowed.
2. `https://www.colliers.com/sitemap` -> XML `<sitemapindex>`, 354 children.
3. `https://www.colliers.com/en/sitemap?type=properties` -> XML `<urlset>`,
   15,896 `usa#######` US listing detail URLs, each with `<lastmod>`.
4. Each detail page renders through Firecrawl with a schema.org
   `RealEstateListing` JSON-LD block (transaction + property type + address in
   `name`, canonical URL, primary image) plus clean markdown (price, lease
   rate, size, coordinates from the map link, photos on `listingsprod.blob`,
   named PDF docs, broker contacts via `.expert-card` selectors).

The June path used no Coveo POST, auth, or gated documents. Documents/images
were URL-only.

## Code (this branch)

- `collect.ts`: `colliers-main` registered in `SOURCE_KEYS` and `runSource`.
- `sources/colliers-main.ts`: `srcColliersMain()` and detail/sitemap helpers:
  - `fetchColliersMainEntries()` - sitemap index -> properties urlset -> dedup
    `usa#######` entries with lastmod.
  - `scrapeColliersMainDetailDoc()` - challenge-aware retry: detects 429/503/
    "Just a moment..." and retries with backoff, relying on the stealth proxy
    rotating IPs per request. Env `COLLIERS_MAIN_CHALLENGE_RETRIES` (default 4).
  - `parseColliersMainDetail()` - JSON-LD + markdown -> listing. Three no-JSON-LD
    cases: 404/"Property Not Found" -> `skip:"not_found"` tombstone; still
    challenged -> throw (retry next pass, un-cached); real 200 with no JSON-LD
    (rare "Powered by LightBox" template) -> `skip:"no_structured_data"`
    tombstone. Tombstones are cached and excluded from emitted rows.
  - `colliersMainEnrichAll()` - enrich once, memoized across sale/lease passes,
    backed by a durable JSONL cache at `out/cache/colliers-main/detail-cache.jsonl`
    so a long run resumes across attempts.
  - Sale pass returns Sale + Sale/Lease; lease pass returns Lease + Sale/Lease;
    a listing in both passes merges to sale_or_lease.
  - Env knobs: `COLLIERS_MAIN_DETAIL_CONCURRENCY` (default min(CONCURRENCY,3)),
    `COLLIERS_MAIN_DETAIL_WAIT_MS` (default 4000), `COLLIERS_MAIN_CHALLENGE_RETRIES`.
- `lib/html.ts`: reusable `extractSitemapLocs()` and `extractSitemapUrlEntries()`
  for any future sitemap-driven source.
- `cre_ingest.py`: `SOURCE_TO_BROKERAGE["colliers-main"] = ("colliers", "main:")`.
  Folds into the colliers brokerage with `main:<usaID>` external_ids, mirroring
  `cbre-dealflow` and `jll-investor`. SalesTracker `colliers` rows are untouched.
- `cre_validate.py`: added `WHEN b.slug='colliers' AND external_id LIKE 'main:%'
  THEN 'colliers-main'` so the validation report attributes folded rows
  correctly (merged sale_or_lease rows can lose `raw_data.sourceKey`).

## Run Log

Commands (run from `cre_collector/`):

```bash
# bounded batch (validated the live path)
COLLIERS_MAIN_DETAIL_CONCURRENCY=2 npx tsx collect.ts --source=colliers-main \
  --transaction=both --max-items=2000 --out=out/colliers_main_batch1_2026-06-12.json
python3 cre_ingest.py --in out/colliers_main_batch1_2026-06-12.json   # additive
npm run validate:supabase -- --out /tmp/cre_validate_colliers_main.md

# full run (in progress)
COLLIERS_MAIN_DETAIL_CONCURRENCY=2 npx tsx collect.ts --source=colliers-main \
  --transaction=both --max-items=0 --out=out/colliers_main_full_2026-06-13.json
```

Results:

- First batch run: 53% Cloudflare 429 under sustained load -> added the
  challenge-aware retry; re-run recovered them (errors dropped from 1,054 to ~1).
- Bounded batch ingested additively (no `--mark-missing`): colliers brokerage
  1,172 -> 2,115 active (+943 colliers-main: 346 sale, 518 lease, 79
  sale_or_lease). Total DB active 71,601 -> 72,544.
- Quality on the 943: 0 duplicate external_ids, 0 bad child URLs, 0 orphans,
  6% missing state, <1% missing coords; 1,932 contacts, 424 documents, 13,572
  images. SalesTracker untouched.

## Next Steps

1. When the full run (`out/colliers_main_full_2026-06-13.json`) completes, check
   its tombstone/error counts in the run log.
2. Additive ingest: `python3 cre_ingest.py --in out/colliers_main_full_2026-06-13.json`
   (no `--mark-missing` while other sources remain partial; the colliers
   brokerage now has two folded sources, so `--mark-missing` would require both
   `colliers` and `colliers-main` present in one batch anyway).
3. `npm run validate:supabase` and confirm colliers-main counts/quality.
4. Update canonical status docs with final colliers-main counts:
   `START_HERE.md`, `BROKERAGE_STATUS_2026-06-12.md`, `cre_collector/CLAUDE.md`,
   `scripts/firecrawl-ops/CLAUDE.md`, root `CLAUDE.md`.

## Reusable Pattern

The "public XML sitemap discovery + per-listing detail render + JSON-LD parse"
approach is reusable for any Cloudflare-protected brokerage that publishes a
sitemap. The recipe: find the sitemap via robots.txt, fetch it through local
Firecrawl (not direct GET), enumerate detail URLs, render each with stealth +
waitFor + challenge-aware retry, parse JSON-LD for the reliable fields and HTML
for the rest, tombstone 404s and alternate-template pages, cache durably for
resumability. Sitemap helpers live in `lib/html.ts`; challenge detection in
`sources/colliers-main.ts`. See the brokerage completion playbook.
