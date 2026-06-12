# JLL Scraper Notes

Production bulk collection uses the rendered JLL search pages in `cre_collector/collect.ts`. This Python scraper is retained for parser and detail-page experiments.

## Search Pages

- Public sale and lease inventory is exposed through rendered listing pages.
- Cards link to detail pages under JLL property paths.
- Firecrawl needs enough wait time for hydrated cards.

## Known Split

JLL Investor Center is handled separately in the production collector as `jll-investor`. Keep investment-center logic out of this legacy scraper unless a new shared JLL module is created.

## 2026-06-12 JLL Investor Center Notes

Scope: source key `jll-investor` only. This is distinct from the main `jll`
property search source and appears to be investment-sale oriented, not a lease
feed.

### Bounded probe commands and artifacts

All probes were small and wrote only `/tmp` artifacts. No binaries were
downloaded and no Supabase ingest was run.

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh

curl -L -sS --compressed -A 'Mozilla/5.0' \
  -D /tmp/jll_investor_headers.txt \
  -o /tmp/jll_investor_search.html \
  'https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D'

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D' \
  --formats rawHtml,markdown,links --wait-for 8000 --timeout 120000 \
  --out /tmp/jll_investor_fc_search.json \
  --save-fields /tmp/jll_investor_fc_search_fields --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D&page=22' \
  --formats rawHtml,markdown,links --wait-for 8000 --timeout 120000 \
  --out /tmp/jll_investor_fc_search_page22.json \
  --save-fields /tmp/jll_investor_fc_search_page22_fields --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/us/en/listings/living-multi-housing/ora-apartments' \
  --formats rawHtml,markdown,links --wait-for 8000 --timeout 120000 \
  --out /tmp/jll_investor_detail_ora.json \
  --save-fields /tmp/jll_investor_detail_ora_fields --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/sitemap_index.xml' \
  --formats rawHtml,markdown,links --wait-for 1000 --timeout 60000 \
  --out /tmp/jll_investor_fc_sitemap_index.json \
  --save-fields /tmp/jll_investor_fc_sitemap_index_fields --quiet --print-paths
```

Additional saved artifacts:

- `/tmp/jll_investor_fc_search_page2.json`
- `/tmp/jll_investor_fc_search_page3.json`
- `/tmp/jll_investor_fc_search_page23.json`
- `/tmp/jll_nextdata_fc_page2.json`
- `/tmp/jll_investor_detail_fox_hill.json`
- `/tmp/jll_investor_detail_quickchek.json`
- `/tmp/jll_investor_fc_html_sitemap.json`
- `/tmp/jll_investor_fc_robots.json`
- `/tmp/jll_investor_fc_sitemap_us.json`

### Findings

- Direct `curl` to `invest.jll.com` returned Akamai `403` for the search page,
  robots, sitemap index, and `_next/data` JSON. Local Firecrawl rendered the
  same paths successfully.
- `robots.txt` is reachable through local Firecrawl and includes
  `Disallow: */property-search?*`, `Disallow: */profile/*`, and
  `Sitemap: https://invest.jll.com/sitemap_index.xml`. This is a policy issue
  for a defensible bulk collector because the easiest complete discovery route
  is a query-string search URL.
- The current collector captures only the first rendered grid. The public
  search surface has real pagination:
  - Page 1: `advancedSearch.count` drifted between 1,086 and 1,088 during the
    probe window, with 50 listing rows.
  - Page 2: 50 listing rows.
  - Page 22: 38 listing rows.
  - Page 23: 0 listing rows.
  This indicates roughly 1,088 current United States search rows, not 50.
- The rendered search page includes `__NEXT_DATA__` with
  `initialState.advancedSearch.listings`, `count`, `filters`, and selectable
  filters. Each listing row includes stable Salesforce-style `id`, `alias`,
  `name`, `displayAddress`, city, state, country, lat/lon, asset type, deal
  type, under-contract flag, image URL, and selected facts such as area,
  land area, or price when public.
- The Next.js JSON route also works through local Firecrawl:
  `/_next/data/<buildId>/us/en/property-search.json?...`. It returned pure JSON
  for page 2 with `count=1088` and 50 listing rows. Direct `curl` to the same
  route was blocked by Akamai. The build id should be read from the current
  rendered page before using this route because it changes across deploys.
- No separate public XHR, GraphQL endpoint, or stable non-Next API was found in
  the bounded probes. The usable data surfaces were embedded `__NEXT_DATA__`,
  `_next/data/<buildId>/...json` through Firecrawl, and detail-page
  `__NEXT_DATA__`.
- `/sitemap.xml` returned a 404 page. `/sitemap_index.xml` exists, and the US
  sitemap at `/us/sitemap-us.xml` rendered through Firecrawl with 1,862
  `/us/en/listings/...` URLs. The sitemap is not United States inventory only;
  it includes all listings in the US locale, including non-US locations. It can
  be a compliant detail-URL discovery seed, but detail scraping or another
  filter is required to keep only United States listings.
- The HTML sitemap at `/us/en/sitemap` is public and lists filter links by
  asset type, deal type, country, and state. It does not expose detail listing
  URLs directly.

### Detail-page public data

Three detail pages were probed: ORA Apartments, Fox Hill, and QuickChek
Carlstadt.

- Detail pages expose `initialState.pdp.listing` in `__NEXT_DATA__`.
- Public listing fields include id, alias, name, city, state, country,
  latitude, longitude, asset type, deal type, status or under-contract flag,
  description, highlights, date published, date modified, and source-specific
  facts such as price, raw price, cap rate, NOI, occupancy, building area, land
  area, unit count, year built, tenancy, and call-for-offers date when present.
- Public media is richer on detail pages than on search rows. Sample image
  counts from `multimedia.images`: ORA Apartments 6, Fox Hill 7, QuickChek 1.
- Public broker data is exposed in `brokers`, including visible broker names,
  titles, emails, phone numbers, and avatar/image URLs. Firecrawl links did not
  expose separate broker profile URLs in the sampled pages.
- Public document URLs vary by listing. ORA Apartments and Fox Hill exposed one
  public teaser/flyer PDF each in `documents.teaser`. QuickChek exposed no
  teaser document in the sampled JSON.
- `documentsCA` exposes CA/NDA PDF URLs. Treat these separately from offering
  memoranda or brochures. They should not be counted as public deal documents
  without a policy decision.
- Deal room language appears in the app, and QuickChek showed a "Deal room"
  label in markdown. Full deal-room contents are registration or approval
  gated and should remain out of scope for the URL-only public collector.

### Status recommendation

Operationally, `jll-investor` is not limited to a first public grid. A full
public search pagination path is feasible through local Firecrawl, and detail
pages expose enough public JSON for useful URL-only enrichment.

For EQUIRE's defensible dataset, keep the status as **Partial** until one of
these is approved:

1. Use the query-string search pagination despite the robots disallow line,
   with an explicit policy/legal decision.
2. Use the XML sitemap as the discovery seed, then scrape detail pages and
   filter to United States listings. This is cleaner relative to robots, but it
   requires many more detail scrapes and will include non-US candidates before
   filtering.

### Collector patch plan

Keep this logic scoped to source key `jll-investor`; do not mix it with the
main `jll` search source.

1. Replace card-anchor parsing in `srcJllInvestor` with a parser for
   `__NEXT_DATA__.props.pageProps.initialState.advancedSearch`.
2. Paginate sale only with `page=1..N`, using `count / 50` as guidance and
   stopping on an empty `listings` array. Keep `PAGE_CAP` as a hard cap.
3. Optionally use `_next/data/<buildId>/us/en/property-search.json?...` through
   local Firecrawl after reading the current `buildId` from page 1. Fall back
   to rendered search HTML if the JSON route shape changes.
4. Map search rows to collector fields: stable id, URL from `alias`, name,
   transaction type `Sale (investment)`, asset type, display address, city,
   state, country, lat/lon, status, price or price range, area, land area,
   image URL, dates, and raw row.
5. Add bounded detail enrichment for selected or all rows, with low concurrency:
   parse `initialState.pdp.listing`, append public `multimedia.images`, public
   teaser/flyer document URLs, and broker contact data. Store CA/NDA URLs
   separately in `raw_data` or omit them from `documents` until policy approves
   them as collectible URLs.
6. Keep lease behavior as `skipped` with note `Investment-sale platform; no
   lease inventory`.
7. Verification path:
   - `npx tsx collect.ts --source=jll-investor --transaction=sale --max-items=120 --page-cap=3 --concurrency=2 --out=/tmp/jll_investor_page_probe.json`
   - `python3 cre_ingest.py --in /tmp/jll_investor_page_probe.json --dry-run --keep-artifacts /tmp/jll_investor_ingest_check`
   - Full run only after the robots/policy route is chosen.

### 2026-06-12 collector hardening and run prep

Scope: source key `jll-investor` only. This pass did not expand search
pagination and did not change the policy-sensitive discovery path. It hardened
the current first rendered search page by parsing embedded `__NEXT_DATA__` and
enriching the already discovered public detail URLs.

Commands:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh

cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=jll-investor --transaction=both --max-items=8 --page-cap=2 --concurrency=2 --out=out/jll_investor_probe_current_2026-06-12.json
npm run typecheck
npx tsx collect.ts --source=jll-investor --transaction=both --max-items=8 --page-cap=2 --concurrency=2 --out=out/jll_investor_probe_fixed_2026-06-12.json
python3 cre_ingest.py --in out/jll_investor_probe_fixed_2026-06-12.json --dry-run --keep-artifacts out/jll_investor_ingest_fixed_2026-06-12
```

Saved artifacts:

- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_current_2026-06-12.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_current_2026-06-12.log`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_search_2026-06-12.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_search_2026-06-12_fields/`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_detail_ora_2026-06-12.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_detail_ora_2026-06-12_fields/`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_enriched_2026-06-12.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_enriched_2026-06-12.log`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_fixed_2026-06-12.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_fixed_2026-06-12.log`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_ingest_fixed_2026-06-12/ingest.sql`

Results:

- Current pre-patch probe: 8 sale listings, 0 lease listings, source total
  unknown, 0 broker records, 8 rows with one search-card image, 0 rows with
  contacts, 0 rows with documents.
- Fixed probe: 8 sale listings, 0 lease listings, source total 1,087 at run
  time, 19 deduped broker records.
- Fixed detail coverage: 8/8 rows with image URLs, 8/8 rows with contact data,
  8/8 rows with latitude/longitude, 4/8 rows with public teaser document URLs.
- Sample ORA Apartments coverage: stable Salesforce-style id
  `006Vk00000A8hrqIAB`, 6 image URLs, 4 public broker contacts, 1 public
  offering teaser PDF URL, and CA/NDA document URLs retained only in
  `jllInvestorDetail.documentsCA`.
- Dry-run ingest staged 8 `jll-investor` listings, skipped 0 for missing URL,
  wrote `out/jll_investor_ingest_fixed_2026-06-12/ingest.sql`, and did not
  connect to Supabase.

Collector behavior after patch:

- Sale path parses `initialState.advancedSearch.listings` from
  `__NEXT_DATA__` instead of relying on card text only.
- Sale path enriches each selected detail URL by parsing
  `initialState.pdp.listing` from public detail-page `__NEXT_DATA__`.
- Stored child rows remain URL-only: public teaser/flyer PDFs in `brochures`,
  image URLs in `photos`, and broker contact fields in `contactsDetailed`.
- `documentsCA` URLs are not promoted to public document child rows. They are
  retained in raw detail metadata pending a policy decision.
- Lease remains a supported skip with note `Investment-sale platform; no lease
  inventory.`

Remaining blocker:

The source remains **Partial**. A complete run still needs a policy decision on
whether to use the query-string search pagination path despite the
`robots.txt` `Disallow: */property-search?*` line, or to switch to XML-sitemap
detail discovery and United States filtering.

## 2026-06-12 Deep Dive Notes

Scope: public JLL Property listings at `property.jll.com`, source key `jll`. This does not cover JLL Investor Center except as a separate source handled by `jll-investor`.

### Commands and artifacts

All probes used local Firecrawl at `http://localhost:3002`, did not download binaries, and did not ingest to Supabase.

- Health and compile checks:
  - `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh`
  - `cd scripts/firecrawl-ops/cre_collector && npm run typecheck`
- Current collector sample:
  - `npx tsx collect.ts --source=jll --transaction=both --max-items=3 --page-cap=2 --concurrency=2 --out=out/jll_probe_2026-06-12/jll_collect_max3.json`
- Search page Firecrawl probes:
  - `scripts/firecrawl-ops/firecrawl_request.py scrape 'https://property.jll.com/search?tenureTypes=sale&page=1' --formats rawHtml,markdown,links --out scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_search_sale_p1.json --pretty --quiet --print-paths`
  - Same shape for rent, plus waited `curl` scrape calls with `waitFor: 8000`.
  - Property type probes saved as `scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_search_<tenure>_<property-type>_p1_wait8000.json`.
- Detail page Firecrawl probes:
  - `jll_detail_sale_westlake.json`
  - `jll_detail_sale_615_3rd.json`
  - `jll_detail_lease_steuart.json`
  - Parsed extract: `jll_detail_nextdata_extract.json`
- API and bundle probes:
  - Static chunk copies under `scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/chunks/`
  - Direct GraphQL POST attempts saved as `jll_graphql_*.json`
  - Property type count summary: `jll_search_property_type_summary.json`

### Endpoint and path evidence

- Hydrated public search pages work with local Firecrawl when `waitFor` is long enough:
  - `https://property.jll.com/search?tenureTypes=sale&propertyTypes=office&page=1`
  - `https://property.jll.com/search?tenureTypes=rent&propertyTypes=office&page=1`
- Search pages expose 50 listing links per full result page under `/listings/...`.
- The current collector URL omits `propertyTypes`. Hydrated `__NEXT_DATA__` shows JLL defaults that route to `propertyTypes: ["office"]`, so the present `jll` collector is office-only in practice.
- JLL's public page config lists these property type filter values:
  - `office`, `industrial`, `retail`, `land`, `medical`, `multifamily`, `lab`, `coworking`, `data-center`
- Public first-page count probes by filter, before any cross-filter de-dupe:

| Tenure | Property type | Count | First page listing links |
| --- | --- | ---: | ---: |
| sale | office | 333 | 50 |
| sale | industrial | 492 | 50 |
| sale | retail | 220 | 50 |
| sale | land | 765 | 50 |
| sale | medical | 16 | 16 |
| sale | multifamily | 5 | 5 |
| sale | lab | 4 | 4 |
| sale | coworking | 53 | 50 |
| sale | data-center | 1 | 1 |
| rent | office | 4,345 | 50 |
| rent | industrial | 2,577 | 50 |
| rent | retail | 1,391 | 50 |
| rent | land | 304 | 50 |
| rent | medical | 170 | 50 |
| rent | multifamily | 17 | 17 |
| rent | lab | 199 | 50 |
| rent | coworking | 524 | 50 |
| rent | data-center | 4 | 4 |

Observed public filtered counts sum to 1,889 sale and 9,531 rent before cross-property-type de-dupe. The current office-only collector path sees 333 sale and 4,345 rent, so it is partial coverage.

### JSON and detail enrichment

- The browser bundle exposes a public GraphQL surface at `/api/graphql`.
- Useful operations found in the hydrated chunks include:
  - `SearchResults`
  - `getPropertyCount`
  - `getPDPProperty`
  - `getPDPAvailabilities`
  - `getPropertyById`
  - `getSRPPropertyBrokers`
  - `BrokersByCity`
  - `BrokersByEmail`
- Simple direct POST probes from Node to `https://property.jll.com/api/graphql` returned generic HTTP 400 responses, even with browser-like headers and operation names. Treat GraphQL as the best structural path only after reproducing the exact browser request shape or capturing browser-context requests. Firecrawl scrape cannot make this POST path directly.
- Detail pages are immediately useful through `__NEXT_DATA__` in `rawHtml`. The three sampled detail pages exposed:
  - Stable JLL property id
  - Title, address, city, state, postal code, page URL
  - `propertyTypes` and `tenureTypes`
  - Sale or rent price fields, usually "contact us" rather than numeric asking price
  - Surface area fields
  - Description sections, highlights, amenities, custom attributes
  - Latitude and longitude fields
  - Brochure URL arrays
  - Cloudinary image URL arrays
  - Broker arrays with name, email, telephone, job title, market, city, profile slug, photo URL, and LinkedIn URL
- Sample enrichment from saved extracts:
  - Westlake Professional Campus: 1 brochure, 1 image, 1 broker with email, phone, profile slug, photo, LinkedIn
  - 615 3rd Street: 1 brochure, 5 images, 3 brokers with contact and profile fields
  - Steuart Street Tower: 1 brochure, 8 images, 5 brokers with contact and profile fields

### Limitations

- Current production `srcJll` is not full coverage because the JLL site defaults untyped searches to office.
- Counts above are per property type filter and may double-count listings that carry multiple property types or both sale and rent tenure. Collector output must de-dupe by normalized detail URL and JLL property id after detail fetch.
- JLL pricing is often withheld as "Please contact us for price", so numeric price coverage will remain sparse even after detail enrichment.
- Raw HTML scanning is still needed for `__NEXT_DATA__`, because Markdown and Firecrawl links do not expose all broker fields or structured property arrays.
- VCard links were not observed in sampled public JLL detail pages. Broker profile slugs, email, telephone, photo, and LinkedIn are available in embedded JSON.
- Do not download PDF brochures or image binaries. Store URLs only.

### Status recommendation

Status: partial, patch recommended before calling `jll` complete for EQUIRE.

The public path is defensible once the collector loops all public property types and enriches detail pages from `__NEXT_DATA__`. Without that patch, the source is usable for office listings but misses large public sale and lease segments such as land, industrial, retail, lab, medical, coworking, multifamily, and data center.

### Concrete collector patch plan

1. In `srcJll`, define the public property type list from the page config:
   - `office`, `industrial`, `retail`, `land`, `medical`, `multifamily`, `lab`, `coworking`, `data-center`
2. Loop `tenureTypes=sale|rent` and every property type:
   - `https://property.jll.com/search?tenureTypes=<sale|rent>&propertyTypes=<type>&page=<n>`
3. Parse hydrated search pages with `waitFor: 8000`.
4. Parse the displayed count from Markdown or hydrated page text and stop pagination at `ceil(count / 50)`, bounded by `PAGE_CAP`.
5. Extract `/listings/...` URLs from hydrated `rawHtml` and Firecrawl links.
6. De-dupe listing URLs across property types and tenures before detail fetch.
7. Add a bounded detail enrichment pass for each unique JLL listing URL:
   - scrape `rawHtml`, `markdown`, and `links`
   - parse `script#__NEXT_DATA__`
   - read `pageProps.property` and `pageProps.brokers`
8. Map detail fields into the existing listing shape:
   - `source_id` from JLL property id
   - `source_url` from canonical detail URL
   - `title`, address fields, `lat`, `lon`
   - `transaction` from `tenureTypes`, preserving dual rent and sale where present
   - `asset_type` from `propertyTypes`
   - size fields from `surfaceAreas`
   - price fields from `salePrice` or `rentPrice`, with contact-for-price preserved as null numeric price plus raw text if supported
   - description, highlights, amenities, custom attributes
   - document URLs from `brochures` and `floorPlans`
   - image URLs from `images`
   - broker records from embedded broker arrays, including profile slug, email, telephone, photo, and LinkedIn URL
9. Keep `jll-investor` separate. Do not mix Investor Center inventory into source key `jll`.
10. Add a small source-specific probe or fixture that verifies at least one non-office type, for example sale land or rent industrial, so a future regression cannot silently fall back to office-only coverage.

### 2026-06-12 cautious main JLL collector upgrade

Implemented first-pass production collector coverage for source key `jll` only.
The adapter now keeps to rendered public `property.jll.com` search pages and
loops all documented public property type filters for both tenure modes:
`office`, `industrial`, `retail`, `land`, `medical`, `multifamily`, `lab`,
`coworking`, and `data-center`.

The collector uses URLs shaped as:

```text
https://property.jll.com/search?tenureTypes=<sale|rent>&propertyTypes=<type>&page=<n>
```

Behavior:

- Uses `tenureTypes=sale` for sale and `tenureTypes=rent` for lease.
- Parses hydrated rendered cards with `waitFor: 8000`.
- Honors `PAGE_CAP` per property-type filter.
- De-dupes by normalized `/listings/...` URL across filters.
- Preserves per-filter source total evidence in the source note and in each
  listing's raw collector payload.
- Uses round-robin selection across filters so small `--max-items` probes are
  not office-only by accident.
- Does not call GraphQL.
- Does not touch JLL Investor Center routes.

Detail-page enrichment remains intentionally deferred in this pass. Detail
pages should still be the next upgrade path for stable JLL property ids,
brochures, richer images, brokers, coordinates, description, and structured
price or size fields from `__NEXT_DATA__`.

Verification:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=jll --transaction=both --max-items=12 --page-cap=2 --concurrency=2 --out=/tmp/jll_property_types_probe.json
python3 cre_ingest.py --in /tmp/jll_property_types_probe.json --dry-run --keep-artifacts /tmp/jll_property_types_ingest_check
```

Results:

- Typecheck passed.
- Probe wrote 24 total listings: 12 sale and 12 lease.
- Sale per-filter source totals before de-dupe: office 333, industrial 492,
  retail 220, land 765, medical 16, multifamily 5, lab 4, coworking 53,
  data-center 1.
- Lease per-filter source totals before de-dupe: office 4,345, industrial
  2,577, retail 1,391, land 304, medical 170, multifamily 17, lab 199,
  coworking 524, data-center 4.
- The bounded sample included every documented property type token for sale
  and lease. One lease listing appeared under both medical and office, which
  confirmed cross-filter URL de-dupe and filter preservation.
- Ingest dry run staged 24 JLL listings, skipped 0 for missing URL, wrote
  `/tmp/jll_property_types_ingest_check/ingest.sql`, and did not connect to
  Supabase.

### 2026-06-12 main JLL detail enrichment and cache probe

Implemented bounded detail-page enrichment for source key `jll`. The collector
still discovers rows through rendered public `property.jll.com` search pages
across all documented property-type filters, then enriches each selected detail
URL from `script#__NEXT_DATA__`.

Code behavior:

- Detail pages are cached under `out/cache/jll-detail/` by normalized listing
  URL so interrupted long runs do not discard completed rendered detail scrapes.
- `JLL_DETAIL_CONCURRENCY` can override the detail enrichment concurrency
  independently of search-page concurrency. The default remains conservative,
  but the 2026-06-12 full run was restarted with `JLL_DETAIL_CONCURRENCY=6`
  after cache progress showed the browser-rendered detail phase was the
  bottleneck.
- Search pages that render zero cards are retried with longer waits before the
  collector accepts the page, because a full-run attempt briefly returned 0
  cards for sale/industrial even though the verified public total is 492.
- Detail failures remain row-local as `detailError`; the source does not fail
  just because one detail page is weak.
- Stable JLL property ids from `pageProps.property.id` are used as collector
  ids. If the same property appears in sale and lease passes, `cre_ingest.py`
  can merge it into `sale_or_lease` through its existing `(brokerage,
  external_id)` logic.
- URL-only child rows come from public `brochures`, `floorPlans`, `images`, and
  broker profile/avatar fields. No PDFs or images are downloaded.
- Broker profile URLs use the verified public pattern
  `https://www.us.jll.com/en/people/<pageUrl>`; sample `justin-lossner` returned
  `200` through local Firecrawl.

Verification:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=jll --transaction=both --max-items=6 --page-cap=1 --concurrency=2 --out=/tmp/jll_detail_cached_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_detail_cached_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_detail_cached_probe_2026-06-12_ingest_check
```

Probe result:

- 12 listings emitted: 6 sale and 6 lease.
- 0 detail errors, 0 skipped ingest rows, and 0 duplicate collector ids.
- 12 rows had stable JLL property ids and coordinates.
- 10 public document URLs, 37 image URLs, 25 contact rows, and 25 public broker
  profile URLs were emitted.
- The JLL detail cache contained 12 rendered detail files after the probe.

Search retry verification:

```bash
npx tsx collect.ts --source=jll --transaction=sale --max-items=12 --page-cap=1 --concurrency=3 --out=/tmp/jll_search_retry_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_search_retry_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_search_retry_probe_2026-06-12_ingest_check
```

Result: 12 sale rows covered all nine property-type tokens, including
industrial; 0 detail errors; 12 stable ids; 12 public document URLs; 27 contact
rows; and dry-run ingest staged all 12 rows.

Next step:

- Run or resume a full JLL collection with detail cache enabled, high enough
  page cap for office lease (`--page-cap=100` was required on 2026-06-12), and
  `JLL_DETAIL_CONCURRENCY=6` if local Firecrawl remains healthy. Dry-run and
  live ingest only after detail errors and duplicate/merge behavior are
  understood.
