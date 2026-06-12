# Colliers Public Path Recheck - 2026-06-12

Scope: focused recheck of the remaining Colliers inventory path beyond the
already completed SalesTracker subset. No live ingest was run. No binary files
were downloaded. Probes were limited to small GET or HEAD-style checks, saved
artifact inspection, and documentation review.

## Verdict

No safe repeatable public GET or rendered path was found for the remaining
main `www.colliers.com/en/properties` sale and lease inventory beyond
SalesTracker.

The production-safe path remains the existing partial SalesTracker adapter:
`https://sales.colliers.com/` plus public RCM ListingEngine GET endpoints at
`https://my.rcm1.com`. That path covers an investment-sale subset only. It
does not solve the main Colliers Coveo sale and lease inventory.

## Documents Read

- `CLAUDE.md`
- `scripts/firecrawl-ops/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/HANDOFF_LOG_2026-06-11.md`
- `scripts/firecrawl-ops/cre_collector/LESSONS_2026-06-11.md`
- `scripts/firecrawl-ops/cre_collector/VALIDATION_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/SUPABASE_RECENT_UPLOAD_QA_2026-06-12.md`
- `docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`
- `scripts/firecrawl-ops/cre_scrapers/CLAUDE.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/colliers/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/colliers/scraper.py`
- Colliers adapter section in `scripts/firecrawl-ops/cre_collector/collect.ts`

## Safe Probes Run

### Main Colliers direct GET checks

Command shape:

```bash
node --input-type=module <<'NODE'
const urls = [
  'https://www.colliers.com/en/properties',
  'https://www.colliers.com/en/properties?types=industrial',
  'https://www.colliers.com/robots.txt',
  'https://www.colliers.com/sitemap.xml',
  'https://www.colliers.com/coveo/rest/search/v2?numberOfResults=1'
];
// fetch each URL with a normal user-agent, print status, bytes, and signal flags
NODE
```

Result:

- `https://www.colliers.com/en/properties`: HTTP 403 Cloudflare challenge,
  no listing IDs, no SalesTracker links in the returned body.
- `https://www.colliers.com/en/properties?types=industrial`: HTTP 403
  Cloudflare challenge.
- `https://www.colliers.com/robots.txt`: HTTP 403 Cloudflare challenge.
- `https://www.colliers.com/sitemap.xml`: HTTP 403 Cloudflare challenge.
- `https://www.colliers.com/coveo/rest/search/v2?numberOfResults=1`: HTTP 403
  Cloudflare challenge. This was a GET-only sanity check, not a POST replay.

### Indexed detail URL direct GET checks

Web search surfaced indexed Colliers detail pages, including sale and
sale-or-lease examples. Direct collector-style GET checks against those URLs
still failed:

```text
https://www.colliers.com/en/properties/for-sale-commercial-lot-on-major-thoroughfare/usa-9100-brockington-rd-sherwood-ar-72120-usa/usa1155686
https://www.colliers.com/en/properties/development-land-for-sale-fond-du-lac/usa-wisconsin/usa1140882
https://www.colliers.com/en/properties/kapolei-business-park-phase-i-for-lease-or-sale/usa-kalaeloa-blvd-kapolei-hi-96707/usa1092689
https://www.colliers.com/en/properties/for-lease-or-sale-swc-of-route-100-nursery-street-fogelsville/usa-pa-100-nursery-st-upper-macungie-township-pa-18069-usa/usa1140313
```

Result: all returned HTTP 403 Cloudflare challenge pages with no listing
content, no JSON-LD, and no visible document or property-detail markup.

Shortlink checks also failed:

```text
https://www.colliers.com/p-usa1155686
https://www.colliers.com/p-usa1140882
https://www.colliers.com/p-usa1092689
```

Result: all returned HTTP 403 Cloudflare challenge pages.

Interpretation: the main-site listing pages are publicly indexed, but direct
collector access is still not repeatable enough to use as a source feed.
Search-engine snippets are useful discovery evidence, not a production data
source.

### SalesTracker public GET checks

Command shape:

```bash
node --input-type=module <<'NODE'
import * as cheerio from './scripts/firecrawl-ops/cre_collector/node_modules/cheerio/dist/esm/index.js';
// GET sales.colliers.com, extract the RCM engine key, then GET small
// GetListingsHtml and GetMapData windows with XMLHttpRequest-style headers.
NODE
```

Results:

- `https://sales.colliers.com/`: HTTP 200, 14,679 bytes, ListingEngine key
  present.
- `GetListingsHtml Start=1 PageSize=5`: HTTP 200 JSON, `success=true`,
  `total=1653`, `totalAvail=2094`, `numProjects=5`, 5 parsed cards.
- `GetListingsHtml Start=1 PageSize=100`: HTTP 200 JSON, 100 parsed cards.
- `GetListingsHtml Start=1 PageSize=250`: HTTP 200 JSON, 250 parsed cards.
- `GetListingsHtml Start=1301 PageSize=100`: HTTP 200 JSON, 100 parsed cards.
- `GetListingsHtml Start=1601 PageSize=100`: HTTP 200 JSON,
  `numProjects=53`, 53 parsed cards.
- `GetListingsHtml Start=1654 PageSize=100`: HTTP 200 JSON,
  `numProjects=0`, 0 parsed cards.
- `GetMapData Start=1 PageSize=5`: HTTP 200 JSON, 5 map rows, first
  `ProjectId=150540`.
- `GetMapData Start=1601 PageSize=100`: HTTP 200 JSON, 58 map rows, first
  `ProjectId=117109`.

Interpretation: SalesTracker remains safe and repeatable through public GET.
The existing collector behavior matches the public endpoint behavior: the
filtered total reports 1,653, but public cards stop after the final 53-card
window and then return a 0-card page.

The `PageSize=250` probe suggests the list endpoint tolerates larger page
sizes. This could reduce list/map request count, but it does not change the
coverage blocker and is not urgent because the validated full SalesTracker run
already finished in about 3 minutes and 17 seconds. Any page-size change should
be benchmarked in a dry-run-only collector probe before adoption.

### Existing SalesTracker artifact inspection

Artifact:

```text
scripts/firecrawl-ops/cre_collector/out/colliers_salestracker_full_2026-06-12_050241.json
```

Key checks:

```bash
jq -r '{totalListings, sources:.sources, listings:(.listings|length),
  missingState:([.listings[] | select((.state//"")=="")]|length),
  missingCoords:([.listings[] | select((.latitude==null) or (.longitude==null))]|length)}' \
  scripts/firecrawl-ops/cre_collector/out/colliers_salestracker_full_2026-06-12_050241.json

jq -r '.listings[] | select(.name? | test("Triton Cay";"i")) |
  {id,name,city,state,salePriceUsd,sizeText,buildingSizeSqft,lotSizeAcres,salePriceText,colliersSalesTrackerDetail}' \
  scripts/firecrawl-ops/cre_collector/out/colliers_salestracker_full_2026-06-12_050241.json
```

Findings:

- The artifact has 1,300 SalesTracker listings.
- Source metadata records `totalAvailableOnSource=1653` for sale and an
  explicit 0-row lease skip.
- 29 artifact rows have missing state, but 0 rows have missing coordinates.
- The QA outlier `Triton Cay Orlando` has source fields
  `salePriceText=$95,760,000`, `sizeText=872 sq ft`, and
  `buildingSizeSqft=872`. This is likely a source-field semantics issue
  because the asset type is multifamily, not a discovery-path problem.

### Local Firecrawl check

Command attempted:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh >/tmp/firecrawl_healthcheck_colliers_probe_20260612.log 2>&1 &&
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties/for-sale-commercial-lot-on-major-thoroughfare/usa-9100-brockington-rd-sherwood-ar-72120-usa/usa1155686' \
  --formats markdown,links \
  --out /tmp/colliers_detail_firecrawl_probe_20260612.json \
  --pretty --quiet --print-paths
```

Result: not tested. The healthcheck failed before the scrape because OrbStack's
Docker socket was unavailable:

```text
failed to connect to the docker API at unix:///Users/caymanseagraves/.orbstack/run/docker.sock
```

Do not interpret this as evidence that local Firecrawl can or cannot scrape
main-site Colliers detail pages today.

## Blockers

1. Main `www.colliers.com` direct GET access is Cloudflare-challenged for the
   search shell, indexed detail pages, shortlinks, robots, sitemap, and a tiny
   Coveo GET sanity check.
2. The known main-site search workflow is Coveo POST-driven. Replaying that
   without a clear public contract, consent decision, or authorized integration
   remains outside the collector safety boundary.
3. Search-index visibility proves public pages exist, but it does not provide
   a stable complete inventory feed, pagination contract, source totals, or
   refresh semantics.
4. Local Firecrawl could not be retested in this pass because OrbStack was not
   running. Even if detail pages scrape later, discovery remains unsolved
   unless a safe public feed or sitemap becomes accessible.

## Collector Refinement Review

No code patch was made in this pass.

Potential safe refinements to consider later:

- Benchmark `COLLIERS_PAGE_SIZE=250` for SalesTracker. It may reduce list/map
  requests, but full-run speed is already acceptable and detail enrichment is
  the dominant cost.
- Add a conservative title fallback for SalesTracker card-only rows whose
  names end in city/state text, such as `Gilroy, CA`, while keeping source
  coordinates as the stronger location signal. This may reduce the 29 missing
  state rows, but it should be validated against a dry-run artifact first.
- Review multifamily SalesTracker `Size` semantics before treating small values
  as `buildingSizeSqft`. The `Triton Cay Orlando` QA flag is a good target
  because the source says `872 sq ft` on an asset priced like a large
  multifamily offering. This is an accuracy audit item, not evidence of a
  missing public path.

## Recommendations

1. Keep Colliers status as partial: SalesTracker investment-sale subset only.
2. Do not claim main Colliers sale or lease coverage until a safe public
   non-POST path, a Firecrawl-compatible action path with explicit approval, or
   an authorized Coveo integration exists.
3. Do not use search-engine snippets, cached pages, or third-party mirrors as
   production discovery.
4. If OrbStack is started later, run one detail-page-only local Firecrawl probe
   against an indexed `usa######` URL. Avoid rendering the main search page
   during this safety review because it can silently trigger the Coveo POST
   workflow.
5. Keep document and image handling URL-only. Do not classify SalesTracker
   agreement or gated brochure links as public document rows unless their access
   policy is explicitly resolved.
