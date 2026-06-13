# JLL Detail Enrichment Safe Paths - 2026-06-12

Scope: source keys `jll` and `jll-investor` in
`scripts/firecrawl-ops/cre_collector/collect.ts`.

This note is a no-ingest reconnaissance pass. No PDF or image binaries were
downloaded. Direct probes wrote only `/tmp` HTML/header files, and existing
rendered Firecrawl artifacts were used for field-shape analysis because the
local Firecrawl runtime was unavailable during this pass.

## Context Read

Read before probing:

- `CLAUDE.md`
- `scripts/firecrawl-ops/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/HANDOFF_LOG_2026-06-11.md`
- `scripts/firecrawl-ops/cre_collector/LESSONS_2026-06-11.md`
- `scripts/firecrawl-ops/cre_collector/VALIDATION_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`
- `docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/PERFORMANCE_ACCURACY_REVIEW_2026-06-12.md`

## Commands Run

From repo root:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Result: local Firecrawl was not reachable because the OrbStack Docker socket
was unavailable:

```text
failed to connect to the docker API at unix:///Users/caymanseagraves/.orbstack/run/docker.sock
Could not reach http://localhost:3002/v2/scrape: <urlopen error [Errno 61] Connection refused>
```

Attempted, but blocked by the local runtime state:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://property.jll.com/listings/615-3rd-st-cbd' \
  --formats rawHtml,markdown,links --wait-for 8000 --timeout 120000 \
  --out /tmp/jll_detail_615_fresh_2026-06-12.json --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://invest.jll.com/us/en/listings/living-multi-housing/ora-apartments' \
  --formats rawHtml,markdown,links --wait-for 8000 --timeout 120000 \
  --out /tmp/jll_investor_ora_fresh_2026-06-12.json --quiet --print-paths
```

Safe direct HTTP probes:

```bash
curl -L -sS --compressed -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36' \
  -D /tmp/jll_property_615_headers_2026-06-12.txt \
  -o /tmp/jll_property_615_direct_2026-06-12.html \
  'https://property.jll.com/listings/615-3rd-st-cbd'

curl -L -sS --compressed -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36' \
  -D /tmp/jll_investor_ora_headers_2026-06-12.txt \
  -o /tmp/jll_investor_ora_direct_2026-06-12.html \
  'https://invest.jll.com/us/en/listings/living-multi-housing/ora-apartments'

curl -I -L -sS --compressed -A 'Mozilla/5.0' \
  'https://res.cloudinary.com/jll-global-sandbox/image/upload/v1781194429/MyListing/public/prod/property-338430/615-3rd-street-brochure-2026.pdf'
```

Direct Next data and robots probes:

```bash
curl -L -sS --compressed --max-time 20 -A 'Mozilla/5.0' \
  'https://property.jll.com/_next/data/tl5cilrWr5FNHPh4YNXOZ/listings/615-3rd-st-cbd.json?market=US&slug-with-postcode=615-3rd-st-cbd'

curl -L -sS --compressed --max-time 20 -A 'Mozilla/5.0' \
  'https://property.jll.com/_next/data/tl5cilrWr5FNHPh4YNXOZ/US/listings/615-3rd-st-cbd.json?market=US&slug-with-postcode=615-3rd-st-cbd'

curl -L -sS --compressed --max-time 20 -A 'Mozilla/5.0' \
  'https://invest.jll.com/_next/data/PlWa9qtmU5yfvXfaZdg4g/us/en/listings/living-multi-housing/ora-apartments.json?region=us&locale=en&asset=living-multi-housing&alias=ora-apartments'

curl -L -sS --compressed --max-time 20 -A 'Mozilla/5.0' \
  'https://property.jll.com/robots.txt'

curl -L -sS --compressed --max-time 20 -A 'Mozilla/5.0' \
  'https://invest.jll.com/robots.txt'
```

Existing rendered artifacts inspected:

- `scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_detail_sale_615_3rd.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_detail_sale_westlake.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_detail_lease_steuart.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_detail_summary.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_detail_ora_2026-06-12.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_fixed_2026-06-12.json`

## Probe Findings

- Direct `curl` to both JLL hosts returned Akamai `403 Access Denied`:
  `property.jll.com/listings/615-3rd-st-cbd`,
  `invest.jll.com/us/en/listings/living-multi-housing/ora-apartments`,
  both tested `_next/data` routes, and both `robots.txt` URLs.
- The public Cloudinary brochure URL from the 615 3rd Street detail page
  responded to `HEAD` with `HTTP/2 200`, `content-type: application/pdf`,
  `content-length: 5156756`, `access-control-allow-origin: *`, and an ETag.
  This confirms that discovered brochure URLs are public URL targets, but the
  collector should still store URL rows only and should not download binaries.
- Existing rendered Firecrawl artifacts remain the right evidence source for
  detail enrichment because they contain hydrated `script#__NEXT_DATA__` while
  direct HTTP does not.

## Main JLL Field Shape

Sample URL:

```text
https://property.jll.com/listings/615-3rd-st-cbd
```

Rendered artifact:
`scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_detail_sale_615_3rd.json`.

Observed Next metadata:

- `buildId`: `tl5cilrWr5FNHPh4YNXOZ`
- `page`: `/_markets/[market]/listings/[slug-with-postcode]`
- query: `market=US`, `slug-with-postcode=615-3rd-st-cbd`
- useful page props: `property`, `brokers`, `schemaData`, `breadcrumbs`,
  `relativeUrl`

`pageProps.property` exposes the important enrichment fields:

- Stable property id: `338430`
- Title and canonical page URL: `615 3rd Street`, `/listings/615-3rd-st-cbd`
- Location: address, city, state, postcode, latitude `41.590209`, longitude
  `-93.620878`
- Classification: `propertyTypes=["office"]`, `tenureTypes=["rent","sale"]`
- Price and size fields: `salePrice`, `rentPrice`, `surfaceArea`,
  `surfaceAreas`, `hidePrice`, `hideAvailabilitiesPrice`
- Rich text and facts: `descriptionSections`, `highlights`, `amenities`,
  `customRefId`, `buildingClass`, `parkingDetails`, `locationDescription`,
  `submarket`
- Assets: `brochures`, `floorPlans`, `images`, `videos`, `virtualTours`,
  `view360URLs`

`pageProps.brokers` exposes public broker data:

- `id`, `name`, `email`, `telephone`, `jobTitle`, `office`, `city`, `market`
- `pageUrl` profile slug, for example `justin-lossner`
- `photo` Cloudinary avatar URL
- `linkedin`
- `brokerLicenses` and `entityLicenses`

Sample 615 3rd Street structured coverage from the saved artifact:

- 1 brochure URL
- 5 property image URLs
- 3 brokers
- 0 VCard URLs observed
- Link extraction included brochure and search/filter URLs, but the complete
  broker and property field shape came from `__NEXT_DATA__`

## JLL Investor Center Field Shape

Sample URL:

```text
https://invest.jll.com/us/en/listings/living-multi-housing/ora-apartments
```

Rendered artifact:
`scripts/firecrawl-ops/cre_collector/out/jll_investor_detail_ora_2026-06-12.json`.

Observed Next metadata:

- `buildId`: `PlWa9qtmU5yfvXfaZdg4g`
- `page`: `/[region]/[locale]/listings/[asset]/[alias]`
- query: `region=us`, `locale=en`, `asset=living-multi-housing`,
  `alias=ora-apartments`
- useful page props: `initialState.pdp.listing`

`initialState.pdp.listing` exposes:

- Stable Salesforce-style id: `006Vk00000A8hrqIAB`
- Alias: `living-multi-housing/ora-apartments`
- Name: `ORA Apartments`
- Location, coordinates, asset type, deal type, stage, date published, and date
  modified
- `documents.teaser`, 1 public offering teaser URL in the ORA sample
- `documentsCA`, 2 CA/NDA URLs in the ORA sample
- `multimedia.images`, 6 image URLs in the ORA sample
- `brokers`, 4 public broker records

Broker records include public names, titles, emails, phone numbers, avatar
URLs, LinkedIn URLs, licensed entity data, and license lists. No profile URL or
VCard URL was observed in the ORA detail JSON. The current collector correctly
keeps `documentsCA` in raw detail metadata and does not promote those URLs to
public document child rows.

## Investor Center Limitations

The current `jll-investor` collector is enriched but still bounded to the first
rendered search page. Prior notes show:

- Search `advancedSearch.count` around 1,087 to 1,088 United States rows.
- First page has 50 rows.
- Page 22 has 38 rows, page 23 has 0 rows.
- The search route is query-string based and prior rendered robots evidence
  had `Disallow: */property-search?*`.

Completion needs a policy choice:

1. Use search pagination through local Firecrawl despite the query-string
   robots caveat, with explicit approval.
2. Use XML sitemap detail discovery, then filter detail pages to United States
   inventory. This is cleaner relative to the prior robots note but requires
   many more detail scrapes and will include non-US candidates before filtering.

Do not silently expand `jll-investor` to full pagination until that choice is
made.

## Low-Risk Collector Refinements

Recommended main `jll` patch:

1. Keep the current all-property-type search pagination and URL de-dupe.
2. Add opt-in detail enrichment, for example `JLL_DETAIL_ENRICHMENT=1`, rather
   than making every routine probe pay detail-scrape cost.
3. Add `JLL_DETAIL_CONCURRENCY`, capped at 2 or 3, and cache detail results by
   normalized URL so sale, lease, and property-type duplicates are scraped once.
4. Scrape detail pages with `rawHtml`, `markdown`, and `links`, then parse
   `script#__NEXT_DATA__`.
5. Map `pageProps.property.id` to the stable source id before ingest so future
   duplicate handling can prefer JLL's id over URL slugs.
6. Populate coordinates, description sections, highlights, amenities,
   brochures, floor plans, image URLs, and `contactsDetailed`.
7. Populate broker refs from name, email, phone, profile slug, avatar, and
   LinkedIn. Build profile URLs only if a public canonical profile URL pattern
   is proven; otherwise retain the slug in raw data.
8. Keep VCard empty unless a public VCard URL is observed.
9. If a detail scrape fails, keep the search-card row and store `detailError`.
10. Verify on a bounded sample containing at least one non-office sale and one
    non-office lease row, then dry-run ingest only.

Recommended `jll-investor` refinement:

1. Leave first-page enrichment in place.
2. Keep lease as a supported skip.
3. Do not promote `documentsCA` to public document rows without policy approval.
4. If policy approves search pagination, implement page loop with count-guided
   `ceil(count / 50)` stopping and `PAGE_CAP` as a hard cap.
5. If policy prefers sitemap discovery, collect detail URLs from the sitemap,
   scrape details at low concurrency, and filter to U.S. rows from
   `initialState.pdp.listing.country`.

## Status Recommendation

- Main `jll`: keep status as **Needs deep audit**. Search coverage is much
  better after the property-type loop, but rows remain card-level until detail
  enrichment is patched and proven.
- `jll-investor`: keep status as **Partial**. First-page detail enrichment is
  working, but full discovery is a policy-limited choice.
- Production posture: local Firecrawl is a research and local collector aid,
  not a Vercel runtime dependency. The deployed listing product should consume
  Supabase rows or a server-side collector output, not call local Firecrawl.

