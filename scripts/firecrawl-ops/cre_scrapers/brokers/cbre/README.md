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
