# Marcus & Millichap Scraper Notes

Production bulk collection has limited Marcus & Millichap support in `cre_collector/collect.ts`.

## Site Structure

- The public property grid is investment-sale oriented.
- Lease inventory is not public in the same way and is skipped by the production collector.
- Some deal-room or financial details require registration.

## Current Limitation

The public collector should only ingest visible card and detail data. Do not synthesize financial fields that are gated or absent.

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

Upgrade Marcus from "rendered first grid only" to "API-backed public sale feed candidate, with gated-document limits." The site has a reliable public API path beyond the current 12 first-grid rows, but full coverage is not a single unfiltered page loop because the public list endpoint caps at the newest 100 matching rows.

Best collector path:

1. Use `POST /api/contentsearch/mapproperties` to discover all public sale `ActivityId` values and coordinates.
2. Use bounded, retrying calls to `POST /api/contentsearch/mappropertydetail` to recover each listing URL and card HTML.
3. Parse card HTML for external id, title, property type, location, price text, cap rate, image URL, and flags.
4. Optionally enrich selected or full sale detail pages for address, description, specifications, contacts, profile URLs, emails, phones, avatar URLs, gallery image URLs, and gated deal-room URLs.
5. Keep documents and images as URLs only. Do not download OM, PDF, or image binaries.
6. Keep lease skipped with an evidence note. Add auctions only if EQUIRE wants auction inventory as a separate sale-like sub-source or transaction subtype.

### Collector patch plan

- Replace the current `srcMarcusMillichap` rendered-page scrape with a POST helper for `properties`, `mapproperties`, and `mappropertydetail`.
- Preserve a small `properties` call as a sanity check for total count, facets, and newest-100 behavior.
- Add a `parseMarcusTileHtml` helper so both `properties.Results.Properties[].Tile` and `mappropertydetail.Results.PropertyDetail` share parsing.
- Use `DealId` from tile HTML or `PropertyUrl` as the stable external id, with `ActivityId` in `raw_data`.
- Add `enrichMarcusDetailPage(url)` for public HTML fields, contacts, images, and gated deal-room URL classification. Detail failures should not drop the feed row.
- Keep `tx === "lease"` returning skipped, with the 2026-06-12 no-public-lease evidence in the note.
- If auctions are added, use a prefixed id such as `auction:<dealId>` or a separate source key so auction rows cannot collide with standard sale rows.
