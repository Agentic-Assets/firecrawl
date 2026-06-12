# Savills Broker Notes

## Latest Recheck

See `RECHECK_2026-06-12.md` for the latest Savills-only recheck. It found a
defensible public U.S. commercial lease path with 2 Chicago retail listings and
patched the collector lease path accordingly. Savills sale remains partial and
not CRE-defensible because the current 100 collected sale rows come from the
global residential property search, while the corrected commercial sale route
only exposed a Toronto, Canada sale object.

## 2026-06-12 Continuation Probe

Scope: Savills-only follow-up for the CRE collector. No secrets were read, no
Supabase ingest was run, no binary assets were downloaded, and `collect.ts` was
not edited because the live issue is source suitability rather than an isolated
parser bug.

### Current Public Endpoints Checked

- Sale list: `https://search.savills.com/com/en/list/property-for-sale/united-states-of-america`
- Lease list: `https://search.savills.com/com/en/list/property-to-rent/united-states-of-america`
- U.S. corporate service sample:
  `https://www.savills.us/services/occupier-services/site-selection.aspx`
- U.S. capital markets sample:
  `https://www.savills.us/services/capital-markets.aspx`
- Sale detail sample:
  `https://search.savills.com/com/en/property-detail/gbssofslo260011`
- Lease fallback detail sample:
  `https://search.savills.com/com/en/property-detail/cyelit10899`

### Commands Run

Health check:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Direct public HTML probes:

```bash
curl -LsS -A 'Mozilla/5.0' \
  'https://search.savills.com/com/en/list/property-for-sale/united-states-of-america' \
  -o /tmp/savills_sale_live_20260612.html

curl -LsS -A 'Mozilla/5.0' \
  'https://search.savills.com/com/en/list/property-to-rent/united-states-of-america' \
  -o /tmp/savills_lease_live_20260612.html

curl -LsS -A 'Mozilla/5.0' \
  'https://search.savills.com/com/en/property-detail/gbssofslo260011' \
  -o /tmp/savills_sale_detail_gbssofslo260011_20260612.html

curl -LsS -A 'Mozilla/5.0' \
  'https://search.savills.com/com/en/property-detail/cyelit10899' \
  -o /tmp/savills_lease_detail_cyelit10899_20260612.html

curl -LsS -A 'Mozilla/5.0' \
  'https://www.savills.us/services/occupier-services/site-selection.aspx' \
  -o /tmp/savills_us_site_selection_20260612.html

curl -LsS -A 'Mozilla/5.0' \
  'https://www.savills.us/services/capital-markets.aspx' \
  -o /tmp/savills_us_capital_markets_20260612.html
```

Targeted collector probes:

```bash
cd scripts/firecrawl-ops/cre_collector

npx tsx collect.ts --source=savills --transaction=sale \
  --max-items=12 --page-cap=4 --concurrency=1 \
  --out=/tmp/savills_sale_probe_20260612.json

npx tsx collect.ts --source=savills --transaction=lease \
  --max-items=12 --page-cap=4 --concurrency=1 \
  --out=/tmp/savills_lease_probe_20260612.json
```

### Fresh Results

- Local Firecrawl health check passed. Compose printed expected warnings for
  unset optional env vars, then API root and scrape smoke were healthy.
- Sale list HTML was 838,959 bytes, reported `105` properties for sale, exposed
  `19` unique property-detail links on page 1, and had `rel=next` pointing to
  `/page/2`.
- Lease list HTML was 413,183 bytes, exposed one distinct detail URL
  (`cyelit10899`), had no reported U.S. lease count, and had no `rel=next`.
- Sale collector probe wrote `/tmp/savills_sale_probe_20260612.json`, collected
  `12` sale listings from page 1, and reported source total `105`.
- Lease collector probe collected `0` U.S. listings. It logged page 1 and page 2
  as zero collected, then exited with `Error: no listings collected from any
  source` because the run was lease-only and empty. This is consistent with the
  current lease path being a non-U.S. fallback rather than U.S. inventory.
- The first sale detail JSON-LD says `10790 Bellagio Rd` is a `Product` with
  `description: "7 bedrooms House for sale"`, `priceCurrency: "USD"`, and
  `streetAddress: "10790 Bellagio Rd, Bel-Air, California, CA 90077"`.
- Sale and lease detail pages each exposed one `.pdf` URL, but it was the
  generic Savills app terms PDF:
  `https://pdf.savills.com/Savills-App-Terms-of-Use-v1.15.01.2026.pdf`.
  No property brochure or OM PDF was observed in these two samples.
- The sale detail sample exposed many image URLs and one `tel:` link, but the
  contact/profile-like links were global Savills or contact-form links, not a
  clear U.S. CRE listing broker profile.
- The sampled `www.savills.us` commercial pages exposed service, research,
  office, people, and site-search links. They did not expose a public U.S.
  commercial listing feed, property inventory endpoint, or safe public JSON API.

### Artifacts

- `/tmp/savills_sale_live_20260612.html`
- `/tmp/savills_lease_live_20260612.html`
- `/tmp/savills_sale_probe_20260612.json`
- `/tmp/savills_sale_detail_gbssofslo260011_20260612.html`
- `/tmp/savills_lease_detail_cyelit10899_20260612.html`
- `/tmp/savills_us_site_selection_20260612.html`
- `/tmp/savills_us_capital_markets_20260612.html`

`/tmp/savills_lease_probe_20260612.json` was not written because the lease-only
collector probe produced no listings and exited nonzero.

### Current Recommendation

Do not spend collector-code time enriching the current Savills global
property-search feed unless Cayman explicitly wants residential/global luxury
property rows in EQUIRE. For CRE coverage, the strongest next action is to
temporarily exclude Savills from CRE completeness claims and look for an
authorized or clearly public Savills U.S. commercial inventory source. The
current public paths are useful evidence, but they are not a defensible U.S.
commercial brokerage listing feed.

## 2026-06-12 Deep Dive Notes

Scope: read-only Savills investigation for EQUIRE CRE listing coverage, source
key `savills`. No Supabase ingest was run, no binaries were downloaded, and no
collector code was edited.

### Status

Savills should not be treated as a defensible US CRE listing feed yet.

The current collector path uses Savills global property search:

- Sale: `https://search.savills.com/com/en/list/property-for-sale/united-states-of-america`
- Lease: `https://search.savills.com/com/en/list/property-to-rent/united-states-of-america`

The sale path is a server-rendered residential or global property feed, not a
clean commercial brokerage inventory. The sale page exposes residential category
links such as houses, flats, villas, penthouses, townhouses, vineyards, chalets,
new homes, and studios. A sampled detail page JSON-LD described `10790 Bellagio
Rd` as a `7 bedrooms House for sale`.

The Savills US corporate site at `https://www.savills.us/` maps to commercial
services, research, sectors, offices, people, and case studies, but bounded
Firecrawl search, map, and homepage probes did not reveal a public US commercial
listing inventory endpoint.

### Endpoint And Path Evidence

Current sale path:

- Reports `105 Properties for sale`.
- Across a bounded 12-page audit, exposed `105` distinct property-detail URLs.
- Of those, `100` parsed as US with the current or simple location heuristics.
- The five rejected sale detail URLs are likely parser failures, not proof of
  foreign inventory. Examples included Big Sky with no second address line,
  `Garrison, NY10524`, `Kinderhook, NY12106`, `Lenox Hill, Manhattan,, NY10021`,
  and `100017`.
- Pages can shuffle or repeat result ordering. A fresh collector run captured
  `97` sale rows even though the same source reports `105`.

Current lease path:

- Does not expose a US lease total in the page body.
- Across pages 1 through 5, exposed one distinct detail URL:
  `https://search.savills.com/com/en/property-detail/cyelit10899`.
- That card is a Cyprus fallback with `Egkomi`, `Nicosia`, and euro monthly
  pricing.
- No alternate public Savills US lease listing endpoint was found in bounded
  local Firecrawl search or map probes.

Detail pages:

- Six representative detail pages were scraped with `rawHtml`, `markdown`, and
  `links`.
- Each sample exposed image URLs and contact/profile-like URLs.
- No sample exposed PDF or brochure URLs.
- Contact links were often global or UK-oriented, for example Savills people,
  Jersey office, and contact form URLs.

### Commands And Artifacts

Health and code validation:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
```

Fresh collector probe:

```bash
npx tsx collect.ts --source=savills --transaction=both \
  --max-items=0 --page-cap=12 --concurrency=1 \
  --out=/tmp/savills_probe_both_pagecap12.json
```

Observed result:

- Sale: `97` listings collected, source total `105`.
- Lease: `0` listings collected, source total unknown.
- Lease note: `3 non-US fallback card(s) filtered out`.

Saved local artifacts:

- `/tmp/savills_probe_both_pagecap12.json`
- `/tmp/savills_page_audit.json`
- `/tmp/savills_detail_audit.json`
- `/tmp/savills_sale_base.json`
- `/tmp/savills_lease_base.json`
- `/tmp/savills_us_home.json`
- `/tmp/savills_us_map.json`
- `/tmp/savills_find_property.json`

Artifact summaries:

```bash
jq '{sale:{uniqueUrls:.sale.uniqueUrls, uniqueUsUrls:.sale.uniqueUsUrls,
  uniqueNonUsUrls:.sale.uniqueNonUsUrls, pageCount:(.sale.pages|length)},
  lease:{uniqueUrls:.lease.uniqueUrls, uniqueUsUrls:.lease.uniqueUsUrls,
  uniqueNonUsUrls:.lease.uniqueNonUsUrls, pageCount:(.lease.pages|length)}}' \
  /tmp/savills_page_audit.json
```

Result:

- Sale: `105` unique URLs, `100` US-parsed URLs, `5` rejected URLs, `12` pages.
- Lease: `1` unique URL, `0` US-parsed URLs, `1` rejected URL, `5` pages.

```bash
jq '{detailRows:(.rows|length), pdfCounts:[.rows[].pdfCount],
  imageCounts:[.rows[].imageCount],
  profileCounts:[.rows[]|.profileLinks|length],
  telCounts:[.rows[]|.telLinks|length]}' \
  /tmp/savills_detail_audit.json
```

Result:

- Detail rows: `6`.
- PDF counts: all `0`.
- Image counts: all `6`.
- Profile/contact-like link counts: all `3`.
- Tel link counts: `1` or `2`.

Residential category evidence:

```bash
jq '{saleResidentialCategories:[.data.links[] |
  select(test("houses-for-sale|flats-for-sale|villas-for-sale|penthouses-for-sale|townhouses-for-sale|vineyards-for-sale|chalet-for-sale|new-homes-for-sale|studios-for-sale"))] | unique}' \
  /tmp/savills_sale_base.json
```

### Limitations

- Current `savills` source is not proven CRE. It appears to be global or
  residential property search inventory.
- Current lease path appears empty for US inventory and falls back to a Cyprus
  detail card.
- Search-page ordering can shuffle, so the current collector stop condition can
  undercount even the residential/global sale path.
- Current parser drops US-looking sale rows when state and ZIP are joined, when
  address punctuation is unusual, or when the page omits a second address line.
- List-page collector does not currently enrich from detail pages.
- Detail pages have useful image and contact/profile URLs, but no PDFs were
  observed in the six-page sample.

### Concrete Collector Patch Plan

Recommended EQUIRE path:

1. Disable or exclude `savills` from the CRE ingestable set until a public US
   commercial listing feed is found.
2. Keep the current notes as evidence that Savills US commercial services exist,
   but a public US CRE listing feed has not been located.
3. If a future Savills US commercial feed is found, add it as a new audited path
   rather than relying on the current global residential search URL.

If Cayman decides to keep the current Savills property-search feed anyway:

1. Collect all detail URLs first, including filtered URLs, before creating
   listings.
2. Continue paging until `seenDetailUrls.size >= totalAvailable`, a hard
   `PAGE_CAP` is reached, or a larger consecutive no-new-detail-url threshold is
   reached. Do not use accepted-listing count alone as the stop condition.
3. Improve location parsing for joined state and ZIP strings such as `NY10524`,
   double-comma strings such as `Manhattan,, NY10021`, ZIP-only strings, and
   title-only rows such as Big Sky.
4. Add detail-page fallback parsing from JSON-LD `address.streetAddress`,
   `offers.price`, `offers.priceCurrency`, `image`, and `description`.
5. Add detail enrichment for property image URLs, contact form/profile URLs, and
   `tel:` URLs. Continue storing URLs only.
6. Keep lease at zero for the current path unless a US lease detail URL appears.
7. Add a source note that the feed is residential/global and should not be
   presented as CRE coverage.
