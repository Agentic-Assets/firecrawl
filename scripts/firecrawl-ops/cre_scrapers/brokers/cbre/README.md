# CBRE Scraper Notes

Production bulk collection uses the CBRE public listings JSON endpoint in `cre_collector/collect.ts`. This Python scraper is legacy support for source-specific experiments.

## Search API

- Endpoint pattern: `https://www.cbre.com/listings-api/propertylistings/query`
- Important filters:
  - `site=us-comm`
  - `Common.Aspects=isSale` for sale
  - `Common.Aspects=isLetting` for lease
  - `PageSize=200`
  - `Page=<1,2,3,...>`
- The endpoint returns `DocumentCount` and `Documents`.
- Local Firecrawl should use stealth proxy settings for CBRE.

## Data Shape

Rows carry address, coordinates, charges, agent data, brochure URLs, photos, usage type, and area fields. Normalize against the collector vocabulary before ingest.

## 2026-06-12 CBRE Deal Flow Notes

Scope: bounded public probes only for source key `cbre-dealflow`. No binaries were downloaded and no Supabase ingest was run. Probe artifacts were saved under `/tmp/cbre-dealflow-probe/`.

### Recommendation

Status should move from "partial first public grid" to "needs collector patch, public API pagination available." CBRE Deal Flow is not a clean sale-only source: the public filters include `Investment Sale` and `Leasing`, and a small filter probe returned 2,042 investment-sale rows, 27 leasing rows, 1,616 United States rows, 1,566 United States investment-sale rows, and 18 United States leasing rows. The unfiltered public listing endpoint reported 2,142 visible rows and `totalAvail=2550`; the gap needs interpretation before using `totalAvail` as a coverage claim.

### Public API And Pagination

- Homepage direct HTML is an ASP.NET shell with no cards, but it exposes a public Real Capital Markets listing engine key in `new ListingEngine({ key: ... })`.
- `GET /api/Handler/ListingEngine/Config?pv=<key>&callback=listingCallback` returns the public engine config, including `Name="Public CBRE Deal Flow"`, `DefaultPageSize=25`, `WrapperUrl=/partner/Portal/Login/Login.aspx`, and the public listing settings.
- `POST /api/Handler/ListingEngine/GetFilters?pv=<key>` returns public filter facets. Relevant facets include `ProjectType`, `AssetType`, `State`, `City`, `Country`, `Status`, `LeadBroker`, and `Broker`.
- `POST /api/AjaxEngine/GetListingsHtml?&pv=<key>` accepts `Start` and `PageSize`. Probes succeeded for `PageSize=24`, `50`, `100`, and `200`. `Start=1`, `25`, `49`, and `2125` returned listings. `Start=2149` returned `numProjects=0`, consistent with `total=2142`.
- `POST /api/AjaxEngine/GetMapData?pv=<key>` with `Start=1&showall=1` returned `numProjects=2142` and `projectLocations=2407`; map data includes `ProjectId`, latitude, and longitude, but not full listing detail.
- `/robots.txt`, `/sitemap.xml`, `/sitemap_index.xml`, and `/sitemap` returned 404 pages. No sitemap path was found in this pass.

### Public Detail Pages

Public card URLs use `/handler/landing.aspx?pv=<listingPv>` and redirect to `/handler/modern.aspx?pv=<listingPv>`. Detail pages include a large embedded `var data = {...}` object. In three sampled details, the JSON included:

- Stable `projectid`, `pagePvValue`, name, status, project type, address, coordinates, asset type, summary, size, unit count, photo URLs, section image URLs, section text, and contacts.
- Contact names, titles, phone numbers, and emails in the JSON. Some sampled contacts had `ShowEmail=false`, so collector ingestion should only store email when `ShowEmail=true` or a visible `mailto:` exists.
- No broker profile URLs in the sampled details. `ExpertBioUrl` was empty in all three samples.
- Public image URLs are available as `/files/...` paths and should be stored as URLs only.
- Public section links can exist, for example a financing link and a visible `mailto:` in one sampled page.

### Registration And Document Gates

The sampled details had `isUserLoggedIn=false` and a `loggedinuser.agreementlabel` of `CONFIDENTIALITY AGREEMENT`. `brochurelink` and `executivesummarylink` were empty for the anonymous session. The agreement URL redirected to `/buyer/findprofile?...`, which rendered only a generic Real Capital Markets shell in the anonymous probe. Treat offering memoranda, full deal-room documents, and financial detail as gated unless a public URL is visibly exposed on the detail page.

### Collector Patch Plan

1. Replace the homepage rendered-grid parser in `srcCbreDealflow` with the public RCM listing API path.
2. Extract the engine key from the homepage at runtime, falling back to the known public key only if the page shape is unchanged and extraction fails.
3. Fetch filters once and record available `ProjectType`, `Country`, and `Status` facets in `raw_data`.
4. Page `GetListingsHtml` with `PageSize=200` and `Start += numProjects` until `numProjects=0`, respecting `--max-items` and `--page-cap`.
5. Parse each returned HTML card for detail URL, title, transaction label, asset type, city, state, country, visible contact text, size text, and thumbnail URL.
6. For detail enrichment, fetch a bounded set of detail pages with concurrency controls and parse embedded `var data`. Keep the feed row if detail fetch fails and store `detailError` in `raw_data`.
7. Classify transaction type from `ProjectType` or visible card/detail text: `Investment Sale` to sale, `Leasing` to lease, and unknown to raw-only/manual review. Do not hard-code every row as sale.
8. Store public image and document URLs only. Do not follow or download `/files/...` binaries.
9. Store contact email only when `ShowEmail=true` or a visible `mailto:` link exists. Store phone/title/name when visible or when the detail JSON marks them displayable.
10. Keep registration-gated agreement, brochure, executive summary, and deal-room links out of document rows unless they are public direct asset URLs.

### 2026-06-12 Collector Implementation And Validation

Status: implemented in `scripts/firecrawl-ops/cre_collector/collect.ts` for source key `cbre-dealflow`. The collector now uses the public Real Capital Markets ListingEngine path directly, not the first rendered homepage grid. It extracts the public engine key from the homepage, fetches public filters, and calls:

- `POST /api/Handler/ListingEngine/GetFilters?pv=<key>` with a non-empty form body.
- `POST /api/AjaxEngine/GetListingsHtml?&pv=<key>` with `Start`, `PageSize`, and `FilterProjectType`.
- `FilterProjectType=Investment Sale` for sale.
- `FilterProjectType=Leasing` for lease.

Required commands run from `scripts/firecrawl-ops/cre_collector`:

```bash
npx tsx collect.ts --source=cbre-dealflow --transaction=both --max-items=6 --out=/tmp/cbre_dealflow_before_probe.json
npm run typecheck
npx tsx collect.ts --source=cbre-dealflow --transaction=both --max-items=10 --out=/tmp/cbre_dealflow_after_probe.json
python3 cre_ingest.py --in /tmp/cbre_dealflow_after_probe.json --dry-run --keep-artifacts /tmp/cbre_dealflow_after_ingest_check
```

Results:

- Before probe, old collector: 6 sale rows, 0 lease rows, method `Rendered public homepage grid parsed (cards)`.
- Current public filter totals: 2,042 `Investment Sale` rows, 27 `Leasing` rows, and `totalAvail=2550` across all project types.
- After probe, new collector: 20 rows total, 10 sale and 10 lease, 58 unique brokers.
- Ingest dry-run: staged 20 rows, skipped 0 no-URL rows, wrote `/tmp/cbre_dealflow_after_ingest_check/ingest.sql`, did not connect.
- Generated SQL folds this sub-source into parent `cbre` via `dealflow:` external IDs, for example `dealflow:150532`.
- After-probe artifact had 6 public brochure-link cards stored as URL-only document rows, 437 image URLs, and 56 contact rows. No binary assets were downloaded.

Observed limits and guardrails:

- One sale row, `Intown Collection`, exposed a public card but no parseable anonymous `var data` detail object. The collector keeps the card row and stores `detailError` instead of dropping it.
- Some lease cards link directly to public `/buyer/brochure?pv=...` URLs rather than `/handler/landing.aspx`. The collector stores the visible brochure URL and card-level contacts, but does not fetch or download the document.
- Detail-page `loggedinuser` agreement, brochure, executive-summary, and deal-room links are not inserted as document rows unless they are directly visible from a public card or public section link.
- Contact emails are stored only from visible `mailto:` card links or detail JSON with `ShowEmail=true`; hidden detail emails remain out.
- Full unbounded runs should keep low concurrency. Detail enrichment uses concurrency 2 and direct public HTTP, so it does not add local Firecrawl queue load.

### 2026-06-12 Full Run And Live Ingest

Command:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=cbre-dealflow --transaction=both --max-items=0 --concurrency=4 --out=out/cbre_dealflow_full_2026-06-12_041740.json
python3 cre_ingest.py --in out/cbre_dealflow_full_2026-06-12_041740.json --dry-run --keep-artifacts /tmp/cbre_dealflow_full_2026-06-12_041740_ingest_check
python3 cre_ingest.py --in out/cbre_dealflow_full_2026-06-12_041740.json --keep-artifacts /tmp/cbre_dealflow_full_2026-06-12_041740_live_ingest
```

Results:

- Artifact: `out/cbre_dealflow_full_2026-06-12_041740.json`, 12.6 MB.
- Log: `out/cbre_dealflow_full_2026-06-12_041740.log`.
- Runtime: 5:58.
- Collected rows: 1,836 total, 1,809 `Investment Sale` rows and 27 `Lease` rows.
- Public filter totals reported by RCM: 2,042 sale and 27 lease. The sale endpoint stopped returning additional public cards after 1,809 observed cards, so the full artifact records the public-card count collected and the larger reported sale total separately.
- Artifact coverage: 1,900 unique brokers, 416 URL-only document rows, 40,213 image URLs, 5,664 detailed contact rows, and 37 nonfatal `detailError` rows where the public card existed but the anonymous detail page did not expose parseable `var data`.
- Dry-run ingest staged 1,836 rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.

Supabase proof after live ingest:

- Active Deal Flow-prefixed rows inside brokerage slug `cbre`: 1,857, including previous additive probe rows retained because `--mark-missing` was not used.
- Deal Flow-prefixed active transaction split: 1,830 sale and 27 lease.
- Quality checks on the active Deal Flow-prefixed subset: 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad states, 0 bad coordinates, 0 bad cap rates, and 0 orphan contacts/documents/images.
- Child rows on active Deal Flow-prefixed listings: 5,597 contacts, 416 documents, and 40,176 images.
- `search_cre_listings('industrial', null, 'TX', null, 'sale')` returned a live CBRE Deal Flow row (`Fort Worth Shallow Bay`) after ingest.
