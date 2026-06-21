Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# JLL Investor Center Public Path Recheck - 2026-06-12

Scope: source key `jll-investor` only. This is a no-ingest, no-code-change
recheck of the defensible public path beyond the current first-page partial
collector state.

Boundaries honored:

- Public URL-only data.
- No PDF or image binary downloads.
- No auth, gated deal-room, agreement, or unsafe external POST path.
- No Supabase ingest.
- No edits to `scripts/firecrawl-ops/cre_collector/collect.ts` or existing
  collector run artifacts.

## Context Read

Read before probing:

- `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`
- `docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/DETAIL_ENRICHMENT_SAFE_PATHS_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/collect.ts`, read-only JLL Investor
  implementation section

Relevant current state:

- `jll-investor` is sale-only and distinct from source key `jll`.
- Current collector parses the first rendered `property-search` page
  `__NEXT_DATA__`, enriches those detail URLs, and keeps CA/NDA URLs only in
  raw detail metadata.
- Current status remains Partial because full discovery needs a policy choice:
  query-string search pagination or XML sitemap detail discovery.

## Commands Run

Healthcheck:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Result: local Firecrawl was healthy at `http://localhost:3002`.

Direct GET status probes:

```bash
probe_dir=/tmp/jll_investor_public_path_2026-06-12_1528
install -d "$probe_dir"

curl -L -sS --compressed --max-time 25 -A 'Mozilla/5.0' \
  -D "$probe_dir/direct_search_p1.headers" \
  -o "$probe_dir/direct_search_p1.body" \
  -w "search_p1\t%{http_code}\t%{content_type}\t%{size_download}\t%{url_effective}\n" \
  'https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D'
```

The same direct GET shape was used for:

- `property-search?...United States...&page=2`
- `property-search?...United States...&page=22`
- `property-search?...United States...&page=23`
- `https://invest.jll.com/robots.txt`
- `https://invest.jll.com/sitemap_index.xml`
- `https://invest.jll.com/us/sitemap-us.xml`
- `https://invest.jll.com/us/en/sitemap`
- `_next/data/<buildId>/us/en/property-search.json?...`

Rendered Firecrawl probes:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D' \
  --formats rawHtml,markdown,links --wait-for 8000 --timeout 120000 \
  --out "$probe_dir/fc_search_p1.json" \
  --save-fields "$probe_dir/fc_search_p1_fields" --quiet --print-paths
```

The same rendered shape was used for search pages 2, 22, and 23.

Robots, sitemap index, US sitemap, and HTML sitemap used shorter waits:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/robots.txt' \
  --formats rawHtml,markdown,links --wait-for 1000 --timeout 60000 \
  --out "$probe_dir/fc_robots.json" \
  --save-fields "$probe_dir/fc_robots_fields" --quiet --print-paths
```

Next.js JSON route probe, using the live build id from rendered search HTML:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/_next/data/PlWa9qtmU5yfvXfaZdg4g/us/en/property-search.json?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D&page=2&region=us&locale=en' \
  --formats rawHtml,markdown,links --wait-for 1000 --timeout 60000 \
  --out "$probe_dir/fc_nextdata_p2.json" \
  --save-fields "$probe_dir/fc_nextdata_p2_fields" --quiet --print-paths
```

Sitemap detail samples:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/us/en/listings/land/alcanena' \
  --formats rawHtml,markdown,links --wait-for 5000 --timeout 90000 \
  --out "$probe_dir/fc_detail_sitemap_sample_1.json" \
  --save-fields "$probe_dir/fc_detail_sitemap_sample_1_fields" --quiet --print-paths
```

Additional detail samples:

- `https://invest.jll.com/us/en/listings/industrial-logistics/warehouse-for-sale-lease-on-main-road-teparak-road`
- `https://invest.jll.com/us/en/listings/land/freehold-land-16-rai-on-thep-krasattri-road-phuket`
- `https://invest.jll.com/us/en/listings/living-multi-housing/ora-apartments`

## Artifacts

All fresh artifacts are in:

```text
/tmp/jll_investor_public_path_2026-06-12_1528/
```

Key files:

- `direct_status.tsv`
- `direct_nextdata_status.tsv`
- `fc_search_p1.json` and `fc_search_p1_fields/`
- `fc_search_p2.json` and `fc_search_p2_fields/`
- `fc_search_p22.json` and `fc_search_p22_fields/`
- `fc_search_p23.json` and `fc_search_p23_fields/`
- `fc_robots.json` and `fc_robots_fields/`
- `fc_sitemap_index.json` and `fc_sitemap_index_fields/`
- `fc_sitemap_us.json` and `fc_sitemap_us_fields/`
- `fc_html_sitemap.json` and `fc_html_sitemap_fields/`
- `fc_nextdata_p2.json` and `fc_nextdata_p2_fields/`
- `fc_detail_sitemap_sample_1.json` through `fc_detail_sitemap_sample_3.json`
- `fc_detail_known_us_ora.json`

## Probe Findings

### Direct HTTP

Plain `curl` with a browser-like user agent returned Akamai `403` for every
tested JLL Investor Center path:

| Path | Direct status |
| --- | ---: |
| Search page 1 | 403 |
| Search page 2 | 403 |
| Search page 22 | 403 |
| Search page 23 | 403 |
| `robots.txt` | 403 |
| `sitemap_index.xml` | 403 |
| `us/sitemap-us.xml` | 403 |
| HTML sitemap | 403 |
| `_next/data` search JSON | 403 |

Conclusion: direct HTTP is not a viable collector route from this host.

### Rendered Search Pagination

Local Firecrawl rendered the search pages and exposed hydrated
`__NEXT_DATA__.props.pageProps.initialState.advancedSearch`.

Fresh rendered evidence:

| Search page | Count field | Listing rows | Unique listing links |
| --- | ---: | ---: | ---: |
| Page 1 | 1,089 | 50 | 50 |
| Page 2 | 1,089 | 50 | 50 |
| Page 22 | 1,089 | 39 | 39 |
| Page 23 | 1,089 | 0 | 50 extracted links, but no rows |

Important detail: page 23 still produced 50 extracted links, but
`advancedSearch.listings` was empty. For collector correctness, trust
`advancedSearch.listings`, not generic link extraction.

The live Next.js `buildId` was `PlWa9qtmU5yfvXfaZdg4g`.

The `_next/data` search JSON route worked through local Firecrawl and returned
pure JSON for page 2. It reported `advancedSearch.count=1087` with 50 listing
rows, while the rendered HTML search pages reported 1,089. This small drift is
consistent with prior observations. Treat the count as guidance and stop on
empty rows.

Expected query-pagination count:

- Current rendered public count: about 1,087 to 1,089 United States rows.
- Current page size: 50 rows.
- Expected final page: page 22, with roughly 37 to 39 rows.
- Page 23 should stop because hydrated row count is zero.

### Robots And Policy Caveat

Rendered `robots.txt` includes:

```text
User-agent: *
Disallow: */profile/*
Disallow: */property-search?*
Disallow: */forgotpassword
Disallow: */signin?*
Sitemap: https://invest.jll.com/sitemap_index.xml
```

This is the main policy caveat. The complete search-pagination route is
operationally feasible, but it uses `property-search?...` query-string URLs
that robots disallows. Do not silently expand the collector to full search
pagination without explicit policy approval.

The `_next/data` JSON route is only an implementation optimization for the same
search state. It should not be used to bypass the policy decision.

### XML Sitemap Discovery

Rendered sitemap evidence:

- `https://invest.jll.com/sitemap_index.xml` exists.
- It points to locale sitemaps such as `/at/sitemap-at.xml`,
  `/au/sitemap-au.xml`, `/ca/sitemap-ca.xml`, and `/us/sitemap-us.xml`.
- `https://invest.jll.com/us/sitemap-us.xml` contained 1,940
  `/us/en/listings/...` URLs in this probe.

The US locale sitemap is not a United States inventory feed. The first three
detail URLs sampled from `/us/sitemap-us.xml` were:

| URL sample | Detail country |
| --- | --- |
| `/us/en/listings/land/alcanena` | Portugal |
| `/us/en/listings/industrial-logistics/warehouse-for-sale-lease-on-main-road-teparak-road` | Thailand |
| `/us/en/listings/land/freehold-land-16-rai-on-thep-krasattri-road-phuket` | Thailand |

Known U.S. detail sample:

| URL sample | Detail country | Images | Public docs |
| --- | --- | ---: | ---: |
| `/us/en/listings/living-multi-housing/ora-apartments` | United States | 6 | 1 |

Expected sitemap-discovery count:

- Discovery seed: 1,940 detail URLs from `/us/sitemap-us.xml`.
- Final U.S. rows: not knowable from the sitemap alone.
- Best expectation is near the search count, about 1,087 to 1,089 rows, after
  scraping details and filtering `initialState.pdp.listing.country == "United
  States"`.
- The path requires hundreds of extra non-U.S. detail scrapes before filtering.

### HTML Sitemap

Rendered `https://invest.jll.com/us/en/sitemap` produced 134 links, all
filter/search links, with zero detail listing URLs. It is useful as a filter
vocabulary reference, not a full discovery route.

## Recommendation

Recommended defensible path: **XML sitemap detail discovery**, then filter
detail pages to United States rows from
`initialState.pdp.listing.country`.

Reasoning:

- It stays aligned with the sitemap advertised in rendered `robots.txt`.
- It avoids relying on the robots-disallowed `property-search?...` route for
  bulk discovery.
- It uses public detail pages that already expose the richest URL-only data:
  stable id, alias, name, country, state, city, coordinates, asset type, deal
  type, status, descriptions, highlights, public teaser document URLs, image
  URLs, and visible broker contacts.
- It preserves the current collector policy of not promoting CA/NDA URLs to
  public document rows.

Tradeoff:

- It is materially less efficient than search pagination because the US locale
  sitemap currently has 1,940 detail URLs and includes non-U.S. listings.
- The collector would need low-concurrency detail scraping, a page/detail cache,
  and resumability before any full run.

Alternative: **query-string pagination**, only with explicit policy approval.

This is the most complete and efficient technical path:

- Render page 1.
- Parse `buildId`, `advancedSearch.count`, and first 50 rows.
- Continue `page=2..N`, bounded by `PAGE_CAP`, with stop-on-empty.
- Optionally use `_next/data/<buildId>/us/en/property-search.json?...` through
  local Firecrawl after reading the current build id from page 1.
- Enrich selected or all details from public detail `__NEXT_DATA__`.

However, this route should remain blocked for production expansion unless the
robots `Disallow: */property-search?*` caveat is explicitly accepted.

Blocked status would be too strong. JLL Investor Center is not technically
blocked. The current best status is:

```text
Partial, with approved next path needed.
Preferred public-defensible path: XML sitemap detail discovery plus U.S.
detail filtering.
Fast path: query-string search pagination, policy approval required.
```

## Implementation Notes For A Future Patch

If sitemap discovery is approved:

1. Keep source key `jll-investor` separate from `jll`.
2. Fetch `https://invest.jll.com/sitemap_index.xml`, then
   `https://invest.jll.com/us/sitemap-us.xml` through local Firecrawl.
3. Extract `/us/en/listings/...` detail URLs only.
4. Scrape details at low concurrency, with durable cache under the collector
   cache tree, not this notes folder.
5. Parse `script#__NEXT_DATA__` and read
   `initialState.pdp.listing`.
6. Filter to `country == "United States"`.
7. Map stable Salesforce-style `id`, source URL, title, transaction
   `Sale (investment)`, asset type, address fields, lat/lon, status, price or
   raw price, size, descriptions, highlights, public teaser document URLs,
   images, and visible broker contacts.
8. Keep `documentsCA` in raw metadata or omit it from row output unless policy
   explicitly approves it as a public URL-only child surface.
9. Leave lease as a supported skip: investment-sale platform, no lease
   inventory.
10. Verify with a bounded run first, then dry-run ingest only.

If query pagination is approved:

1. Parse page rows from `advancedSearch.listings`, not generic link extraction.
2. Use `ceil(count / 50)` as guidance and stop on the first empty row array.
3. Treat count drift between 1,087 and 1,089 as normal source churn.
4. Use `_next/data` only after reading the current `buildId` from rendered
   page 1.
5. Keep detail enrichment and URL-only asset behavior unchanged.

## No-Go Items

- Do not use gated deal-room contents.
- Do not promote CA/NDA agreement URLs into public document child rows without
  explicit policy approval.
- Do not download PDFs or images.
- Do not use direct external POST or authenticated endpoints.
- Do not run live Supabase ingest as part of path discovery.
