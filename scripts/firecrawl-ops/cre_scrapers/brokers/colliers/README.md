# Colliers Scraper Notes

Colliers has TWO production collector sources, both folded into the `colliers`
brokerage:

1. **`colliers` (SalesTracker)** -- public RCM ListingEngine GET endpoints at
   `sales.colliers.com` / `my.rcm1.com`; investment-sale subset (~1,172 rows).
2. **`colliers-main` (main site)** -- the full `www.colliers.com` public
   inventory, unblocked 2026-06-13 via the public XML sitemap (no Coveo POST,
   no auth). Folded with id prefix `main:`. See below.

The old "main site blocked" verdict is HISTORICAL and superseded; the sitemap
path is the production discovery method for the main site. The dated
investigation notes that reached the blocked verdict are in `archive/`.

## Main site (`colliers-main`): public XML sitemap path (durable)

Discovery is GET-only, robots-compliant, and needs no Coveo POST, no auth, and
no gated documents. The earlier rechecks only tried `sitemap.xml` /
`en/sitemap.xml` via direct GET (Cloudflare 403); the unlock is the **bare
`/sitemap` path through local Firecrawl**.

1. `https://www.colliers.com/robots.txt` (via local Firecrawl): `User-agent: *`
   has only `Crawl-Delay: 30` and no `Disallow` for general crawlers (the 72
   `Disallow: /` entries target specific abusive bots). It declares
   `Sitemap: https://www.colliers.com/sitemap`.
2. `https://www.colliers.com/sitemap` (via local Firecrawl): HTTP 200 XML
   `<sitemapindex>` with 354 child sitemaps. The US-properties child is
   `https://www.colliers.com/en/sitemap?type=properties`.
3. `https://www.colliers.com/en/sitemap?type=properties`: HTTP 200 XML
   `<urlset>` with ~15,896 `usa#######` property detail URLs, **every one
   carrying `<lastmod>`** (real refresh semantics; 548 distinct dates confirmed,
   so this supports CHANGED-detection, not only NEW-detection). Slug transaction
   hints are unreliable (~10,160 have none), so **transaction must come from
   detail content, not the slug**.
4. Each detail page renders through local Firecrawl `proxy: stealth` (direct GET
   is Cloudflare 403). Each page exposes a schema.org `RealEstateListing`
   JSON-LD block (`name` = "<Type> For <Sale|Lease|Sublease|Sale or Lease> --
   <street>, <city>, <state> <zip>, USA", `about.category`, canonical
   `usa#######` URL, primary image) plus markdown carrying price (`$ ... USD`),
   `Building Size`, `Land Area`, `Property Status`, a `Related Documents` block
   with named PDF blob URLs, a Google maps link exposing `q=<lat>,<lng>`, and
   photos on `listingsprod.blob.core.windows.net/ourlistings-usa/...`. Broker
   headshots are on `colliersapps.blob.core.windows.net/people/...`.

Etiquette: robots `Crawl-Delay: 30` is advisory for a single crawler identity;
collection runs through the stealth proxy pool (same posture as CBRE/JLL/Cushman).
Keep detail concurrency modest. Documents/images are stored as URLs only.

Monitoring: the sitemap is the cheapest enumeration (2 stealth XML GETs for the
full id set + per-URL lastmod). The durable detail cache should re-render only
URLs whose `lastmod` advanced (see `cre-intelligence-system-design.md` section
14, colliers-main improvement). SalesTracker tolerates `PageSize` up to 250.

## SalesTracker (`colliers`): historical implementation notes

SalesTracker investment-sale coverage is implemented in
`scripts/firecrawl-ops/cre_collector/collect.ts` (`srcColliers`).

Partial path found: Colliers SalesTracker at `https://sales.colliers.com/`
embeds RCM ListingEngine, and these public GET endpoints worked in bounded
probes:

```text
https://my.rcm1.com/api/AjaxEngine/GetListingsHtml?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=5
https://my.rcm1.com/api/AjaxEngine/GetMapData?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=5
https://my.rcm1.com/api/handler/slp/Init?pv=<landing-page-pv>
```

Evidence:

- Direct `www.colliers.com` probes for properties, robots, and sitemap returned
  Cloudflare 403 challenge pages.
- Local Firecrawl rendered the main properties shell, but it showed no matching
  results and exposed only a path toward SalesTracker.
- The main raw HTML contained Coveo Sitecore config and `/coveo/rest`, but no
  `usa######` listing IDs or embedded listing JSON.
- A tiny Coveo GET probe returned a Cloudflare challenge, including through
  local Firecrawl.
- The SalesTracker RCM list endpoint returned `success: true`, `total: 1653`,
  `totalAvail: 2094`, and `numProjects: 5`.
- Pagination worked with `Start=6&PageSize=5`.
- RCM map GET returned coordinates and `ProjectId` values.
- A sample SLP detail GET returned ProjectId `150540`, title
  `Land - 8304 S. Broadway`, Los Angeles location fields, asking price
  `$1,140,000`, project type `Investment Sale`, asset type
  `Land - Multifamily`, 5 gallery image URLs, 1 Colliers contact, and a
  brochure viewer URL. `IsLeasingProject` was false.

Artifacts:

```text
/tmp/colliers_probe_2026-06-12
```

## 2026-06-12 Codex Collector Implementation

Production collector change:

- Added `srcColliers()` in `scripts/firecrawl-ops/cre_collector/collect.ts`.
- Uses only public GET endpoints from `https://sales.colliers.com/` and
  `https://my.rcm1.com`.
- Does not call the main Coveo POST API.
- Does not call agreement, gated brochure, or authenticated paths.
- Stores image, source, contact, brochure-viewer, and agreement URLs as URLs
  or raw metadata only. It downloads no PDFs or images.
- Returns lease as an explicit zero-row skip until a lease-specific public GET
  path is proven.
- Keeps card rows without public SLP detail links by using the map endpoint's
  `ProjectId`; failed detail enrichments are retained as per-listing
  `detailError`.

Exact public endpoints verified:

```text
https://sales.colliers.com/
https://my.rcm1.com/api/AjaxEngine/GetListingsHtml?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=3
https://my.rcm1.com/api/AjaxEngine/GetListingsHtml?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=4&PageSize=3
https://my.rcm1.com/api/AjaxEngine/GetMapData?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=3
https://my.rcm1.com/api/handler/slp/Init?pv=<public-card-detail-pv>
```

Probe artifacts:

```text
/tmp/colliers_probe_2026-06-12_codex/
/tmp/colliers_collector_probe_2026-06-12.json
/tmp/colliers_collector_probe_2026-06-12_ingest/
/tmp/colliers_collector_both_probe_2026-06-12.json
/tmp/colliers_collector_both_probe_2026-06-12_ingest/
```

Commands run:

```bash
node --input-type=module <bounded direct GET probe>
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=colliers --transaction=sale --max-items=3 --page-cap=1 --concurrency=2 --out=/tmp/colliers_collector_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/colliers_collector_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/colliers_collector_probe_2026-06-12_ingest
npx tsx collect.ts --source=colliers --transaction=both --max-items=3 --page-cap=1 --concurrency=2 --out=/tmp/colliers_collector_both_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/colliers_collector_both_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/colliers_collector_both_probe_2026-06-12_ingest
python3 -m py_compile cre_ingest.py
```

Results:

- Direct GET list probe returned HTTP 200 JSON with `success: true`,
  `total: 1653`, `totalAvail: 2094`, and `numProjects: 3` for the first
  `PageSize=3` window.
- Direct GET second page probe with `Start=4&PageSize=3` also returned HTTP 200
  JSON with the same totals and `numProjects: 3`.
- Direct GET map probe returned `projectLocations` for ProjectIds `150540`,
  `150534`, and `150533`.
- Collector sale probe collected 3 listings, source total 1,653, and 6 unique
  brokers.
- Probe listings were `150540`, `150534`, and `150533`: `Land - 8304 S.
  Broadway`, `121 S. Elm Drive`, and `Primewest Pkwy at Franz Rd`.
- The three-row artifact had 4 public contact rows, 9 image URL rows, 0
  document rows, 0 skipped URLs, and 0 `detailError` rows.
- One of the three cards did not expose a public SLP detail link, so the
  collector retained it as a card/map row with a `colliersSalesTrackerDetail`
  skipped note.
- `transaction=both` collected the same 3 sale rows and an explicit 0-row lease
  source entry.
- Dry-run ingest staged 3 Colliers rows and skipped 0 rows for missing URL.
- `npm run typecheck` and `python3 -m py_compile cre_ingest.py` passed.

Non-source error observed:

- The first ad hoc direct probe command failed before network calls because it
  imported `cheerio` from the repo root, where the collector dependency was not
  resolvable. The repeat probe used built-in parsing and produced the artifacts
  above.

Recommendation:

- Treat Colliers as partial, not complete. SalesTracker investment-sale rows are
  safe to collect through public GET. Complete Colliers coverage remains blocked
  until a public GET, authorized request-body Coveo integration, or other safe
  repeatable path covers the main sale and lease inventory.

Partial adapter plan:

1. Complete. Conservative full SalesTracker collection, dry-run ingest, live
   ingest, and Supabase validation were run on 2026-06-12.
2. Continue to treat main Colliers `www.colliers.com/en/properties` sale and
   lease coverage as blocked until a safe public non-POST path is found.

## 2026-06-12 Full SalesTracker Run And Live Ingest

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=colliers --transaction=both --max-items=0 --page-cap=30 --concurrency=2 --out=out/colliers_salestracker_full_2026-06-12_050241.json
python3 cre_ingest.py --in out/colliers_salestracker_full_2026-06-12_050241.json --dry-run --keep-artifacts /tmp/colliers_salestracker_full_2026-06-12_050241_ingest_check
python3 cre_ingest.py --in out/colliers_salestracker_full_2026-06-12_050241.json --keep-artifacts /tmp/colliers_salestracker_full_2026-06-12_050241_live_ingest
```

Collector result:

- Artifact: `out/colliers_salestracker_full_2026-06-12_050241.json`.
- Log: `out/colliers_salestracker_full_2026-06-12_050241.log`.
- Runtime: 3:16.86.
- Public list pages exposed 1,300 unique sale cards before a 0-card page.
- RCM reported `total: 1653` and `totalAvail: 2094`; the collector preserves
  both source totals and the exposed public card count.
- 486 collected cards did not expose a public SLP detail link and were retained
  as card/map rows.
- Artifact detail coverage: 1,207 unique brokers, 2,915 contact rows, 10,036
  image URLs, 0 document rows, 0 missing URLs, 0 missing titles, and 0
  `detailError` rows.
- Duplicate source ProjectIds: 128 repeated ID groups in the public cards. The
  ingestor staged 1,172 unique upsert rows.

Supabase proof:

- Dry-run staged 1,172 unique Colliers rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.
- Active Colliers rows after ingest: 1,172, all sale.
- Active Colliers child rows after ingest: 2,733 contacts, 0 documents, and
  9,908 images.
- Quality checks: 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad
  state codes, 0 impossible coordinates, 0 duplicate external IDs, and 0 orphan
  contacts/documents/images.
- Sample `search_cre_listings('office', null, null, null, 'sale')` returned
  live Colliers rows including `438 South 3rd Street`, `707 Richards`, and
  `630 Comanche Trail`.

Remaining limit (SalesTracker only):

- The SalesTracker run above covers only the public investment-sale subset
  (~1,172 rows). Full main-site sale + lease coverage is now handled by the
  separate `colliers-main` sitemap source documented at the top of this file;
  the "blocked until a non-POST path exists" note here is HISTORICAL.
