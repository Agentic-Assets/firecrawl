# Colliers Main Sale And Lease Path Recheck - 2026-06-12

Scope: bounded recheck of the main Colliers sale and lease inventory at
`https://www.colliers.com/en/properties`.

This pass did not ingest data, did not edit collector code, did not use gated or
authenticated paths, did not replay Coveo POST requests, and did not download
binary documents or images. Probes were limited to public direct GET checks,
local Firecrawl render/search/map checks, and a small SalesTracker contrast
check.

## Verdict

Main Colliers sale and lease coverage remains blocked for production collection.

No safe repeatable public GET, JSON, sitemap, Firecrawl map, or rendered search
path was found for the main `www.colliers.com/en/properties` sale and lease
inventory. Local Firecrawl can still render known detail pages once URLs are
already known, including fresh lease detail examples from search, but that is
detail enrichment only. It is not an inventory feed with totals, pagination, or
refresh semantics.

The safe production path remains partial: Colliers SalesTracker at
`https://sales.colliers.com/` plus public RCM ListingEngine GET endpoints at
`https://my.rcm1.com`. That path is investment-sale oriented and does not cover
the main Colliers sale plus lease search.

## Existing Files Inspected

- `scripts/firecrawl-ops/cre_scrapers/brokers/colliers/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/colliers/scraper.py`
- Colliers section of `scripts/firecrawl-ops/cre_collector/collect.ts`
- Prior notes:
  - `scripts/firecrawl-ops/cre_scrapers/brokers/colliers/COLLIERS_PUBLIC_PATH_RECHECK_2026-06-12.md`
  - `scripts/firecrawl-ops/cre_scrapers/brokers/colliers/MAIN_PROPERTIES_PUBLIC_RECHECK_2026-06-12.md`

Collector alignment: current production Colliers support is SalesTracker-only.
The collector explicitly skips lease with a note that the main Colliers lease
search remains blocked behind the Coveo POST path.

## Artifacts

Probe artifacts were written under:

```text
/tmp/colliers_main_path_recheck_2026-06-12
```

## Local Firecrawl Health

Command:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Observed result: passed. The local API root check, local scrape smoke test, and
core containers were up.

## Direct Public GET Checks

Command shape:

```bash
node --input-type=module <<'NODE'
// Fetch each URL with a browser-like user-agent, print status, bytes,
// content type, and flags for Cloudflare, Coveo, listing IDs, sitemap XML,
// and SalesTracker/RCM markers.
NODE
```

URLs tested:

```text
https://www.colliers.com/en/properties
https://www.colliers.com/en/properties?types=industrial
https://www.colliers.com/en/properties?types=office
https://www.colliers.com/en/properties?types=retail
https://www.colliers.com/en/properties?transactiontype=Lease
https://www.colliers.com/en/properties?transactiontype=Sale
https://www.colliers.com/robots.txt
https://www.colliers.com/sitemap.xml
https://www.colliers.com/en/sitemap.xml
https://www.colliers.com/coveo/rest/search/v2?numberOfResults=1
```

Observed result: every URL returned HTTP 403 Cloudflare challenge HTML. No
listing IDs, inventory JSON, sitemap XML, or server-rendered listing markup was
present.

## Coveo GET Sanity Checks

The rendered page exposes Coveo markers and these paths, but direct GET probes
still do not return usable JSON:

```text
https://www.colliers.com/coveo/rest
https://www.colliers.com/coveo/rest/search
https://www.colliers.com/coveo/rest/search/v2
https://www.colliers.com/coveo/rest/search/v2?numberOfResults=1
https://www.colliers.com/coveo/rest/ua
https://www.colliers.com/coveo/rest/v6/analytics
```

Observed result: all returned HTTP 403 Cloudflare challenge HTML. No POST
request body was replayed or tested.

## Local Firecrawl Render Checks

Rendered main shell:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_path_recheck_2026-06-12/firecrawl_main_properties.json \
  --pretty --quiet --print-paths
```

Rendered hash-filter variants:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties#f:listingtype=[For%20Sale]&f:recenttransactions=[0]' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_path_recheck_2026-06-12/firecrawl_hash_sale.json \
  --pretty --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties#f:listingtype=[For%20Lease]&f:recenttransactions=[0]' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_path_recheck_2026-06-12/firecrawl_hash_lease.json \
  --pretty --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties#f:listingtype=[For%20Sublease]&f:recenttransactions=[0]' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_path_recheck_2026-06-12/firecrawl_hash_sublease.json \
  --pretty --quiet --print-paths
```

Observed result for plain, sale, lease, and sublease variants:

- Firecrawl scrape succeeded.
- Title was `Properties | Colliers`.
- Markdown length was 723 characters.
- Raw HTML length was about 290k characters.
- Links count was 3.
- Listing link count was 0.
- Raw `usa######` listing IDs count was 0.
- Markdown `usa######` listing IDs count was 0.
- A SalesTracker link was present.
- Coveo markers were present, including `/coveo/rest`, `/coveo/rest/ua`, and
  facet hash links for For Sale, For Lease, and For Sublease.
- The rendered markdown ended with `No matching results...`.

Interpretation: local Firecrawl can render the search shell, but it does not
expose inventory cards, listing URLs, source totals, or pagination for the main
Colliers search.

## Local Firecrawl Map Check

Command:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh map \
  'https://www.colliers.com/en/properties' \
  --limit 20 --json --pretty
```

Observed result:

```json
{
  "success": true,
  "data": {
    "links": []
  }
}
```

Interpretation: Firecrawl map did not discover a public listing URL inventory
from the main properties page.

## Known Detail URL Checks

Direct GET against known detail URLs still returned Cloudflare challenge pages:

```text
https://www.colliers.com/en/properties/for-sale-commercial-lot-on-major-thoroughfare/usa-9100-brockington-rd-sherwood-ar-72120-usa/usa1155686
https://www.colliers.com/en/properties/kapolei-business-park-phase-i-for-lease-or-sale/usa-kalaeloa-blvd-kapolei-hi-96707/usa1092689
```

Observed result: both direct GET checks returned HTTP 403 Cloudflare challenge
HTML.

Local Firecrawl rendered both known detail URLs:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties/for-sale-commercial-lot-on-major-thoroughfare/usa-9100-brockington-rd-sherwood-ar-72120-usa/usa1155686' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_path_recheck_2026-06-12/firecrawl_detail_usa1155686.json \
  --pretty --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties/kapolei-business-park-phase-i-for-lease-or-sale/usa-kalaeloa-blvd-kapolei-hi-96707/usa1092689' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_path_recheck_2026-06-12/firecrawl_detail_usa1092689.json \
  --pretty --quiet --print-paths
```

Observed detail results:

- `usa1155686`: Firecrawl success, title was a retail for-sale property, about
  20k markdown chars, 17 links, 10 Colliers listing links, and 1 blob document
  URL.
- `usa1092689`: Firecrawl success, title was a land for-sale-and-lease property,
  about 26k markdown chars, 21 links, 14 Colliers listing links, and 1 blob
  document URL.

Interpretation: known detail pages can be rendered and may provide related
listing hints, but those hints are not a complete, repeatable source inventory.
They lack source totals, complete pagination, and refresh semantics.

## Public Search Discovery Check

Command:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh search \
  'site:colliers.com/en/properties Colliers "For Lease" "usa"' \
  --limit 5 --json
```

Observed result: local Firecrawl search returned indexed Colliers lease detail
URLs, including:

```text
https://www.colliers.com/en/properties/build-to-suit-%E2%94%82-farmington-hills-corporate-campus-up-to-300000-sf-rd-lab-hi-tech-office/usa-build-to-suit-land-farmington-hills-mi-usa/usa1142003
https://www.colliers.com/en/properties/7-eleven-portfolio-of-properties/usa-usa/usa1079188
https://www.colliers.com/en/properties/office-for-lease-three-flexible-suites-built-2006/usa-49078-w-pontiac-trail-wixom-mi-48393-usa/usa1167389
https://www.colliers.com/en/properties/sparks-galleria-shopping-center/usa-pyramid-way-sparks-nv-usa/usa1167392
https://www.colliers.com/en/properties/for-lease-class-a-office-building/usa-401-s-old-woodward-ave-birmingham-mi-48009-usa/usa1137266
```

One fresh lease detail URL was then checked:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties/build-to-suit-%E2%94%82-farmington-hills-corporate-campus-up-to-300000-sf-rd-lab-hi-tech-office/usa-build-to-suit-land-farmington-hills-mi-usa/usa1142003' \
  --formats markdown,links \
  --out /tmp/colliers_main_path_recheck_2026-06-12/firecrawl_search_lease_detail_usa1142003.json \
  --pretty --quiet --print-paths
```

Observed result:

- Direct GET returned HTTP 403 Cloudflare challenge HTML.
- Local Firecrawl rendered the detail page successfully.
- Title was `Office For Lease`.
- Markdown length was about 27k characters.
- Links count was 22.
- Colliers listing link count was 14.
- Document link count was 1.

Interpretation: search can reveal individual indexed detail URLs, and detail
rendering works for known URLs. Search results are still not a production feed:
they provide no complete inventory, no source totals, no stable pagination, and
no authoritative refresh contract.

## SalesTracker Contrast Check

Command shape:

```bash
node --input-type=module <<'NODE'
// GET https://sales.colliers.com/, extract the ListingEngine key, then GET
// small RCM list and map windows with XMLHttpRequest-style headers.
NODE
```

URLs tested:

```text
https://sales.colliers.com/
https://my.rcm1.com/api/AjaxEngine/GetListingsHtml?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=3
https://my.rcm1.com/api/AjaxEngine/GetMapData?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=3
```

Observed result:

- SalesTracker home returned HTTP 200 and exposed ListingEngine key
  `BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4`.
- RCM `GetListingsHtml` returned HTTP 200 JSON with `success=true`,
  `total=1654`, `totalAvail=2096`, and `numProjects=3`.
- RCM `GetMapData` returned HTTP 200 JSON with `success=true`,
  `total=1654`, `totalAvail=2096`, `numProjects=3`, and 3 map rows.

Interpretation: SalesTracker remains the only proven repeatable public GET
Colliers feed. The totals drifted slightly from earlier notes, which is expected
on a live source and does not affect the main-site blocker.

## Practical Recommendation

Keep Colliers status as partial.

Production collector support should remain limited to the already implemented
SalesTracker investment-sale subset. Do not claim main Colliers sale or lease
coverage from `www.colliers.com/en/properties` yet.

Do not implement main-site coverage through Coveo POST replay unless there is
explicit approval for an authorized integration, including request body, consent
posture, throttling, and evidence that the path is an allowed public contract.
Do not use search-engine results as production inventory. Do not classify blob
document URLs found on detail pages as downloaded documents; keep document and
image handling URL-only unless the storage contract changes.

The next safe unlock would be one of:

1. A public inventory GET or JSON endpoint with sale and lease listing URLs,
   totals, and pagination.
2. A public sitemap or feed that is accessible without Cloudflare challenge and
   includes main Colliers sale and lease detail URLs.
3. A local Firecrawl-rendered search path that consistently exposes result
   cards, `usa######` listing URLs, totals, and pagination without hidden or
   unsafe POST replay.
4. An explicitly approved authorized Coveo integration.

Until one of those exists, main Colliers sale and lease discovery remains
blocked.
