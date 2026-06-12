# Marcus & Millichap Scraper Notes

Production bulk collection has Marcus & Millichap support in `cre_collector/collect.ts`.

## Site Structure

- The public property grid is investment-sale oriented.
- Lease inventory is not public in the same way and is skipped by the production collector.
- Some deal-room or financial details require registration.

## Current Limitation

The public collector should only ingest visible card and detail data. Do not synthesize financial fields that are gated or absent.

As of the 2026-06-12 verification pass below, the production collector uses the public map ActivityId route, public `mappropertydetail` tile route, and direct public detail HTML for sale rows, addresses, descriptions, visible advisor contacts, and property image URLs. It still treats Marcus & Millichap as sale-only. Public lease inventory remains unproven. Unfiltered public list search is capped at the newest 100 rows even though the site reports a larger total, so the collector now uses the map ActivityId expansion path instead of relying on list pagination.

## 2026-06-12 Deep Dive Notes

Scope: Marcus & Millichap only, source key `marcus-millichap`. Probes were small and saved under `/tmp/marcus_millichap_probe_20260612/`. No Supabase ingest was run and no PDF or image binaries were downloaded.

### Public API and pagination

- The rendered `/properties` page loads a Vue app from `/Areas/MM/js/bundled-components/PropertySearchResults.js`.
- That bundle exposes public POST endpoints:
  - `POST /api/contentsearch/properties`
  - `POST /api/contentsearch/mapproperties`
  - `POST /api/contentsearch/mappropertydetail`
  - `POST /api/contentsearch/auctions`
  - `POST /api/contentsearch/mapauctions`
  - `POST /api/contentsearch/propertysearchsorttypes`
- The normal property search request body is:

```json
{
  "pageNumber": 1,
  "pageSize": 12,
  "sortOrder": "DESC",
  "indexFieldName": "orderdate",
  "facets": [],
  "rangeFacets": [],
  "geoFacet": { "Polygons": [], "Circles": [], "FieldName": "customdraw" },
  "savedSearchId": null,
  "allowedFacets": ["propertytype", "location", "advisors", "listingprice", "caprate"]
}
```

- Direct POST works without authentication when sent with browser-like headers and JSON content type.
- Evidence from `/api/contentsearch/properties`:
  - Page 1, page size 12: `TotalCount=3136`, `NumberOfPages=9`, 12 rows.
  - Page 2, page size 12: 12 different rows.
  - Page 9, page size 12: 4 rows, `ShowNextPage=false`.
  - Page 10, page size 12: 0 rows.
  - Page 1, page size 100: 100 rows, `NumberOfPages=1`.
- The page itself displays and warns about a newest-100 cap: users must refine searches to see more than the newest 100 matching rows.
- `POST /api/contentsearch/mapproperties` returned 3,136 map records with `ActivityId`, latitude, longitude, and listing flags. It does not include full listing fields.
- `POST /api/contentsearch/mappropertydetail` with a map `ActivityId` returned `PropertyUrl` plus tile HTML. Example: `ZAG0160291` returned `/properties/196042/best-western-inn-of-del-rio`.

### Sale, auction, and lease scope

- Public sale inventory exists through `POST /api/contentsearch/properties`.
- Public auction inventory exists through `POST /api/contentsearch/auctions`; a bounded probe returned `TotalCount=63`, 12 rows on page 1.
- No public lease search mode was found in the page UI or the `PropertySearchResults.js` bundle. The visible navigation offers `Property Sales` and `Auctions`, and the bundle has property and auction endpoints only.
- Negative POST probes to plausible lease endpoints returned 404: `/api/contentsearch/leases`, `/api/contentsearch/leaseproperties`, `/api/contentsearch/propertyleases`, `/api/contentsearch/forlease`, and `/api/contentsearch/lease`.
- Recommendation: treat Marcus as sale-only plus optional auctions. Do not claim public lease coverage unless a new public endpoint or UI mode is found.

### Detail-page enrichment

Sample detail page: `https://www.marcusmillichap.com/properties/196042/best-western-inn-of-del-rio`.

Public HTML included:

- Title, subtype, full address, asking price, cap rate, RevPAR, year built, and long description text.
- 6 property image URLs from `https://mmimageservice.azurewebsites.net/api/image/property/.../L`.
- 5 visible contacts split across listing agents and financing originators.
- Contact names, titles, profile URLs, avatar URLs, phone links, email links, license text where shown, and office/location text.
- A visible deal-room URL: `/properties/196042/deal-room/3299a5a29a3514b3`.

Gated fields:

- The deal-room URL redirects anonymous users to `/mymmi/signin`.
- The detail page also contains a gated modal stating that property details and documents require a free MyMMI account.
- No anonymous direct PDF URL was observed in the sample. Store the deal-room URL as a gated document link only if the downstream schema can distinguish gated URLs from public document URLs.

### Status recommendation

Upgrade Marcus from "rendered first grid only" to "API-backed public sale feed candidate, with gated-document limits." The site has a reliable public API path beyond the original 12 first-grid rows, but full coverage is not a single unfiltered page loop because the public list endpoint caps at the newest 100 matching rows. The production collector now uses `mapproperties` ActivityIds plus `mappropertydetail` tiles to cross that cap.

Implemented collector path:

1. Use a tiny `POST /api/contentsearch/properties` sanity check for public total count and schema drift.
2. Use `POST /api/contentsearch/mapproperties` to discover all public sale `ActivityId` values and coordinates.
3. Use bounded, retrying calls to `POST /api/contentsearch/mappropertydetail` to recover each listing URL and card HTML.
4. Parse card HTML for external id, title, property type, location, price text, cap rate, image URL, and flags.
5. Enrich selected or full sale detail pages for address, description, specifications, contacts, profile URLs, emails, phones, avatar URLs, gallery image URLs, and gated deal-room URLs.
6. Keep documents and images as URLs only. Do not download OM, PDF, or image binaries.
7. Keep lease skipped with an evidence note. Add auctions only if EQUIRE wants auction inventory as a separate sale-like sub-source or transaction subtype.

### Collector patch status

- `srcMarcusMillichap` now uses POST helpers for `properties`, `mapproperties`, and `mappropertydetail`.
- The small `properties` call is preserved as a sanity check for total count, facets, and newest-100 behavior.
- `parseMarcusTileHtml` is reused for public list tiles and public map detail tiles.
- `DealId` from tile HTML or `PropertyUrl` is the stable external id, with `ActivityId` in `raw_data`.
- `enrichMarcusListing(url)` collects public HTML fields, contacts, images, and gated deal-room URL classification. Detail failures do not drop the feed row.
- `tx === "lease"` still returns skipped, with the 2026-06-12 no-public-lease evidence in the note.
- If auctions are added, use a prefixed id such as `auction:<dealId>` or a separate source key so auction rows cannot collide with standard sale rows.

## 2026-06-12 Collector Verification Pass

Scope: Marcus & Millichap only. No live ingest was run. No `--mark-missing` was used. No gated content, auth, binary downloads, OM downloads, PDF downloads, or image downloads were used.

Commands:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=marcus-millichap --transaction=both --max-items=8 --out=/tmp/marcus_before_probe.json
npm run typecheck
npx tsx collect.ts --source=marcus-millichap --transaction=both --max-items=12 --out=/tmp/marcus_after_probe.json
python3 cre_ingest.py --in /tmp/marcus_after_probe.json --dry-run --keep-artifacts /tmp/marcus_after_ingest_check
```

Live public API proof:

- Tiny bounded `POST /api/contentsearch/properties` probe with `pageSize=2` returned HTTP 200 JSON, `TotalCount=3136`, `NumberOfPages=50`, 2 rows, structured listing fields, and embedded tile HTML with `data-activityId` and `data-dealId`.
- `POST /api/contentsearch/mapproperties` returned HTTP 200 JSON and 3,136 public map rows with `ActivityId`, latitude, longitude, and listing flags.
- `POST /api/contentsearch/mappropertydetail` for one ActivityId returned HTTP 200 JSON with `PropertyUrl` and `PropertyDetail` tile HTML.
- The after collector run saw `totalAvailableOnSource=3126`; the public total drifted during the session, so treat this as live source state, not a code discrepancy.

Before patch artifact:

- `/tmp/marcus_before_probe.json`
- 8 top-level sale listings, 0 lease listings.
- 0 global brokers.
- Method was rendered first-page card parsing.
- Fields were limited to card-level id, title, asset type, location, price text, cap rate where visible, one image URL, and URL.

After patch artifact:

- `/tmp/marcus_after_probe.json`
- 12 top-level sale listings, 0 lease listings.
- Sale source summary: method `Public POST /api/contentsearch/properties JSON, newest-100 public cap, plus direct public detail HTML enrichment`, `totalAvailableOnSource=3126`, `listingsCollected=12`.
- Detail enrichment totals: 28 visible `contactsDetailed` records, 64 property image URLs, and 12 gated deal-room URLs retained in raw listing data as `gatedDocuments`.
- Gated deal-room URLs were not mapped into `brochures` because the downstream child document table does not distinguish gated links from public document links.

Ingest dry-run:

- Command staged 12 listings, skipped 0 for missing URL, and wrote `/tmp/marcus_after_ingest_check/ingest.sql`.
- Dry run did not connect to Supabase.

Verification errors and resolution:

- First post-patch typecheck/probe caught a nullable cap-rate parser bug when an API row had no cap-rate text.
- Fixed `parseMarcusTileHtml` to guard null cap-rate text.
- Rerun `npm run typecheck` passed.
- Rerun after probe passed and wrote `/tmp/marcus_after_probe.json`.

Remaining blocked or partial:

- Lease inventory remains unsupported because no public lease UI mode or endpoint has been proven.
- Unfiltered public sale search remains capped at the newest 100 rows. The collector now crosses that cap through public map ActivityIds and `mappropertydetail`; a no-live-ingest 105-row probe passed, but a full 3,126-row collector run and ingest validation are still pending.
- Auctions are publicly discoverable through `/api/contentsearch/auctions`, but are not included in the production Marcus source to avoid mixing standard sale rows with auction inventory without a product decision.

## 2026-06-12 Map ActivityId Expansion Follow-Up

Scope: Marcus & Millichap only. No live ingest was run. No `--mark-missing` was used. No gated content, auth, binary downloads, OM downloads, PDF downloads, or image downloads were used.

Raw public endpoint proof:

- `POST /api/contentsearch/properties` with `pageSize=2` returned HTTP 200, `TotalCount=3126`, `NumberOfPages=50`, 2 structured rows, and the expected public list schema.
- `POST /api/contentsearch/mapproperties` returned HTTP 200 and 3,126 public map rows with `ActivityId`, latitude, longitude, and listing flags.
- `POST /api/contentsearch/mappropertydetail` returned HTTP 200 for sampled ActivityIds at indexes 0, 1, 99, and 100. Index 100 proved a public row beyond the newest-100 list cap:
  `https://www.marcusmillichap.com/properties/301638/7eleven-strip-center-nnn-leases-denver-msa-recent-lease-extension-45-year-hist-occupancy`.

Commands:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=marcus-millichap --transaction=both --max-items=8 --concurrency=3 --out=/tmp/marcus_map_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/marcus_map_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/marcus_map_probe_2026-06-12_ingest
npx tsx collect.ts --source=marcus-millichap --transaction=sale --max-items=105 --concurrency=6 --out=/tmp/marcus_map_105_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/marcus_map_105_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/marcus_map_105_probe_2026-06-12_ingest
```

Results:

- Healthcheck passed and local Firecrawl scrape smoke was healthy.
- Typecheck passed.
- 8-row both-mode probe collected 8 sale rows and 0 lease rows; dry-run ingest staged 8 Marcus rows and skipped 0 for missing URL.
- 105-row sale probe collected 105 sale rows from the public ActivityId expansion path; dry-run ingest staged 105 Marcus rows and skipped 0 for missing URL.
- 105-row artifact totals: 0 missing URLs, 267 visible contact rows, 557 image URLs, 0 public brochure/document rows, and 105 gated deal-room URLs retained in raw listing data only.

Next action:

- Run a conservative full Marcus sale collection from the public ActivityId path, then dry-run ingest and inspect staged row counts, child URL counts, and detail-error counts before any live additive ingest. Keep lease skipped unless a public lease UI mode or endpoint is proven.
