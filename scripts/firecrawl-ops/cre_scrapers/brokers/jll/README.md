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

Operationally, `jll-investor` is no longer limited to a first public grid. The
implemented path uses XML sitemap discovery plus public detail-page
`__NEXT_DATA__`, then filters retained rows to public detail country `US`. This
is cleaner than the robots-disallowed query-string search pagination route, but
it is detail-scrape heavy because the U.S. locale sitemap still includes
global inventory.

For EQUIRE's defensible dataset, the full sitemap detail scan, live ingest,
and validation are now complete as of 2026-06-12 22:47 UTC: 1,857 sitemap
detail URLs scanned, 934 U.S. sale rows retained and live-ingested, 50 stale
early-probe rows soft-deleted after user approval. `jll-investor` status is
**Complete**. Do not use query-string search pagination without an explicit
policy/legal decision.

### Collector patch plan

Keep this logic scoped to source key `jll-investor`; do not mix it with the
main `jll` search source.

1. Fetch `https://invest.jll.com/sitemap_index.xml`.
2. Discover and fetch `https://invest.jll.com/us/sitemap-us.xml`.
3. Extract `/us/en/listings/...` detail URLs and de-dupe them.
4. Use `JLL_INVESTOR_SITEMAP_SCAN_LIMIT` for bounded probes. Without that env
   cap, finite `--max-items` scans a wider candidate window to account for
   non-U.S. rows before slicing retained U.S. rows.
5. Parse public detail-page `initialState.pdp.listing`, append public
   multimedia images, public teaser/flyer document URLs, and broker contact
   data. Keep CA/NDA URLs in raw metadata only.
6. Retain only rows whose public detail country normalizes to `US`.
7. Keep lease behavior as `skipped` with note `Investment-sale platform; no
   lease inventory`.
8. Verification path:
   - `JLL_INVESTOR_SITEMAP_SCAN_LIMIT=8 npx tsx collect.ts --source=jll-investor --transaction=sale --max-items=4 --concurrency=2 --out=/tmp/jll_investor_sitemap_probe.json`
   - `python3 cre_ingest.py --in /tmp/jll_investor_sitemap_probe.json --dry-run --keep-artifacts /tmp/jll_investor_ingest_check`
   - Full run only after the runtime cost and U.S. retained count are understood.

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

### 2026-06-12 sitemap/detail expansion proof

See `archive/JLL_INVESTOR_SITEMAP_DETAIL_2026-06-12.md` for the implemented
public sitemap/detail path.

Review probe:

```bash
JLL_INVESTOR_SITEMAP_SCAN_LIMIT=8 npx tsx collect.ts --source=jll-investor --transaction=sale --max-items=4 --concurrency=2 --out=/tmp/jll_investor_sitemap_probe_review_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_investor_sitemap_probe_review_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_investor_sitemap_probe_review_ingest_2026-06-12
```

Result:

- 1,855 sitemap detail URLs found in the latest current-tree probe.
- 8 detail URLs scanned, 3 U.S. rows retained, 0 detail errors.
- 3 public document URLs, 15 image URLs, 6 contacts, and only `US` countries.
- Dry-run ingest staged 3 `jll-investor` rows and skipped 0 missing URLs.
- No live JLL Investor ingest was run.

[COMPLETED 2026-06-12] The remaining blocker above was resolved. The XML-sitemap
detail discovery path was adopted and the full run completed 2026-06-12 22:47 UTC:
1,857 sitemap detail URLs scanned, 934 U.S. sale rows live-ingested, 50 stale
early-probe rows soft-deleted after user approval. Child rows: 2,572 contacts,
345 documents, 5,658 images. No coordinates are available for jll-investor rows;
the Investor detail path does not expose them. The query-string search pagination
path was not used. `jll-investor` status is now **Complete**.

## 2026-06-12 Deep Dive Notes

Scope: public JLL Property listings at `property.jll.com`, source key `jll`. This does not cover JLL Investor Center except as a separate source handled by `jll-investor`.

### 2026-06-12 long-run performance audit and speed patch

A read-only audit of the active full JLL run confirmed search pagination had
completed and detail enrichment was progressing through `out/cache/jll-detail`,
but uncached details were slow because the active process used an 8000 ms
rendered Firecrawl wait for every detail page.

Collector follow-up:

- `JLL_DETAIL_WAIT_MS` now defaults to 1000 ms for JLL detail pages.
- `JLL_DETAIL_FALLBACK_WAIT_MS` defaults to 8000 ms and refreshes a row only
  when the fast scrape does not expose `pageProps.property` in `__NEXT_DATA__`.
- Detail enrichment now logs every 100 rows.
- Existing cache files remain valid and are reused unless a fallback refresh is
  needed.

Verification:

```bash
JLL_DETAIL_CACHE_DIR=/tmp/jll_fast_detail_probe_cache_2026-06-12 JLL_DETAIL_WAIT_MS=1000 JLL_DETAIL_FALLBACK_WAIT_MS=8000 JLL_DETAIL_CONCURRENCY=2 npx tsx collect.ts --source=jll --transaction=sale --max-items=2 --page-cap=1 --concurrency=2 --out=/tmp/jll_fast_detail_probe_2026-06-12.json
npm run typecheck
python3 -m py_compile cre_ingest.py cre_validate.py && python3 -m compileall -q ../cre_scrapers
```

Result: 2 JLL sale rows, 2 document URLs, 7 image URLs, 4 contact rows, 0
detail errors, 0 missing URLs, TypeScript typecheck passed, and Python compile
checks passed. This patch does not affect an already-running Node process
until that process is restarted or a future run starts.

### 2026-06-12 full detail ingest proof

The fast-detail full run completed from the existing detail cache plus new
tail scrapes:

```bash
JLL_DETAIL_WAIT_MS=1000 JLL_DETAIL_FALLBACK_WAIT_MS=8000 JLL_DETAIL_CONCURRENCY=6 npx tsx collect.ts --source=jll --transaction=both --max-items=0 --page-cap=100 --concurrency=6 --out=out/jll_full_detail_enriched_2026-06-12.json
python3 cre_ingest.py --in out/jll_full_detail_enriched_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_full_detail_enriched_ingest_dry_run_2026-06-12
python3 cre_ingest.py --in out/jll_full_detail_enriched_2026-06-12.json --keep-artifacts /tmp/jll_full_detail_enriched_live_ingest_2026-06-12
```

Result:

- 11,230 collected rows, 10,604 staged unique rows, and 0 skipped missing URLs.
- 0 detail errors.
- 9,747 public document URLs, 28,254 image URLs, 23,801 contact/profile URLs.
- Live ingest completed without broad `--mark-missing`.
- Narrow cleanup soft-deleted 4,406 stale same-URL rows from the older shallow
  JLL run, leaving JLL Investor untouched.
- Active main JLL after cleanup: 10,741 rows, 1,247 sale, 8,733 lease, and 761
  sale_or_lease.
- Remaining 135 duplicate source URL groups are latest-batch sale/lease
  same-page variants.

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

Observed public filtered counts sum to 1,889 sale and 9,531 rent before cross-property-type de-dupe. The current office-only collector path sees 333 sale and 4,345 rent, so it is partial coverage. [SUPERSEDED 2026-06-12: the collector was patched the same day to loop all public property types; full multi-type run completed and live-ingested with 10,741 active rows.]

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

- [SUPERSEDED 2026-06-12] Current production `srcJll` is not full coverage because the JLL site defaults untyped searches to office. The collector was patched the same day to loop all public property types; full multi-type run completed and live-ingested.
- Counts above are per property type filter and may double-count listings that carry multiple property types or both sale and rent tenure. Collector output must de-dupe by normalized detail URL and JLL property id after detail fetch.
- JLL pricing is often withheld as "Please contact us for price", so numeric price coverage will remain sparse even after detail enrichment.
- Raw HTML scanning is still needed for `__NEXT_DATA__`, because Markdown and Firecrawl links do not expose all broker fields or structured property arrays.
- VCard links were not observed in sampled public JLL detail pages. Broker profile slugs, email, telephone, photo, and LinkedIn are available in embedded JSON.
- Do not download PDF brochures or image binaries. Store URLs only.

### Status recommendation

[SUPERSEDED 2026-06-12] Status at time of writing: partial. Main `jll` is now
complete and live-ingested. The collector was upgraded to loop all public
property types with detail enrichment; full run, live ingest, source-scoped
reconciliation, and Supabase validation completed 2026-06-12. Live active rows:
10,741 (1,247 sale, 8,733 lease, 761 sale_or_lease).

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

[COMPLETED 2026-06-12] Full JLL collection with detail enrichment is complete and
live-ingested. The full run used `--page-cap=100` and `JLL_DETAIL_CONCURRENCY=6`.
Active main JLL rows: 10,741 (1,247 sale, 8,733 lease, 761 sale_or_lease).
Source-scoped reconciliation and Supabase validation completed the same day.

### 2026-06-12 full-run performance notes

Why the JLL full run is slower than other brokerages:

- Current complete-detail path renders one public Next.js detail page per
  listing through local Firecrawl.
- Full search discovery found 1,873 unique sale rows and 9,358 to 9,359 unique
  lease rows before same-property merge behavior is applied.
- That means roughly 11,200 detail pages need enrichment if every row is
  detail-complete.
- Other completed sources were faster because they exposed direct JSON APIs,
  public feed pages, or Buildout page caches. JLL's rich detail data currently
  comes from rendered detail HTML.

Speed options explored:

- Direct GraphQL is not proven. Prior probes returned generic HTTP 400 without
  the exact browser request shape.
- `_next/data` looked promising because Firecrawl fetched candidate JSON routes
  in about one to two seconds, but the tested route shapes returned
  `{"notFound": true}` even when built from the cached page's `buildId`, `page`,
  and query values.
- Card-level full runs are much faster, but they lose stable property ids,
  documents, coordinates, richer images, and broker contact/profile fields.
- The practical current speedup is the URL-keyed `out/cache/jll-detail/` cache
  plus `JLL_DETAIL_CONCURRENCY=6`. The cache makes restarts safe and the
  higher detail concurrency was verified on a 12-row lease probe with 0 detail
  errors, 12 stable ids, 12 document URLs, and 24 contacts/profile URLs.

Operational guidance:

- Do not ingest a full JLL artifact produced with `--page-cap=60`; office lease
  needed page 87 on 2026-06-12.
- Use `--page-cap=100` or higher until source totals change.
- If a run must be interrupted, preserve `out/cache/jll-detail/`; restarting
  will reuse completed detail pages.
- If Firecrawl starts producing sustained socket failures under concurrency 6,
  lower `JLL_DETAIL_CONCURRENCY` to 4 and keep the same cache.
