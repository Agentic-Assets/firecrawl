# Transwestern Source Notes

Production bulk collection is implemented in
`scripts/firecrawl-ops/cre_collector/collect.ts` under source key
`transwestern`. These notes capture the 2026-06-12 source audit and live ingest
proof. The collector uses public GET feed URLs and public detail pages only. It
stores PDFs, flyers, images, broker profiles, and VCards as URLs only.

## 2026-06-12 Deep Dive Notes

### Status

Transwestern is no longer blocked on a POST-only path. The public website
exposes a repeatable URL-only GET path for feed discovery and detail enrichment:

- Discovery sitemap: `https://transwestern.com/sitemap.aspx?xml=properties`
- Search/feed JSON: `https://transwestern.com/properties?call=ajax&...`
- Detail pages: `https://transwestern.com/property/<PageUrl>`

The browser source still uses a jQuery `POST` from `/properties`, but the same
`call=ajax` parameters also work as a GET query. A collector can avoid
POST-body dependence.

## 2026-06-12 Full Run And Supabase Proof

Artifacts:

- Raw full artifact: `cre_collector/out/transwestern_full_2026-06-12_121302.json`.
- Cleaned ingest artifact:
  `cre_collector/out/transwestern_full_2026-06-12_121302_cleaned.json`.
- Performance/accuracy review:
  `PERFORMANCE_ACCURACY_NOTE_2026-06-12.md`.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=transwestern --transaction=both --max-items=0 --concurrency=4 --out=out/transwestern_full_2026-06-12_121302.json
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --dry-run --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_ingest_check
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_mark_missing_live_retry
```

Result:

- Raw collection: 2,151 rows, 519 sale-bucket rows and 1,632 lease-bucket rows.
- Staged unique rows: 2,021, after 130 `Sale or Lease` rows merged.
- Detail coverage in the raw artifact: 3,184 document URLs, 5,093 image URLs,
  3,963 contact/profile/VCard URL rows, 0 detail errors, 0 missing URLs, and 0
  missing titles.
- Description cleanup: every raw fallback description matched footer,
  TREC/copyright, or site-map boilerplate, so the cleaned artifact removed 2,151
  descriptions. The collector now has a Transwestern-only guard that emits null
  rather than footer text.
- Live ingest initially failed because the live database had not yet seeded the
  `transwestern` brokerage row already present in `sql/001_cre_brokerages.sql`.
  After seeding that slug, the same cleaned artifact ingested successfully.
- Active Supabase rows: 2,021, with 389 sale, 1,502 lease, and 130
  sale_or_lease.
- Active child URL rows: 3,054 documents, 4,838 images, 3,746 contacts, 3,746
  profile URLs, and 3,746 VCard URLs.
- Validation: 0 bad descriptions, 0 bad document URLs, 0 bad image URLs, 0
  missing URLs, 0 missing titles, 0 missing raw data, 0 duplicate external IDs,
  0 bad state codes, 0 impossible coordinates, 0 malformed guarded prices/cap
  rates, and 0 child orphans.

Remaining refinements:

- Add a run-local detail cache so the 130 `Sale or Lease` detail pages are not
  scraped twice.
- Harden availability table parsing before promoting more sale prices or lease
  rates from detail rows.
- Parse broker office names if a stable contact-card selector is found.

### 2026-06-12 GET Refresh

Fresh bounded probes on 2026-06-12 confirmed the earlier unlock decision. Direct
GET requests returned HTTP 200 and JSON-shaped text responses for the all-feed,
each transaction bucket, and small `search=` probes. The response content type is
`text/html; charset=utf-8`, so parse by body shape rather than content type.

Canonical GET shape:

```text
https://transwestern.com/properties?call=ajax&search=<query>&Latitude=&Longitude=&DealsType=<bucket>&PropertyType=0&MetroName=&SubTypeIDs=&TenancyTypes=&CheckLeed=false&IsEnergyStar=false&MinPrice=&MaxPrice=&MinSize=&MaxSize=&SortType=asc&SortColumn=&class=&TotalLotSizeMin=&TotalLotSizeMax=&NoOfUnitsMin=&NoOfUnitsMax=
```

Useful bounded probes:

```text
search=1800, DealsType= blank -> 8 rows
search=1025, DealsType= blank -> 4 rows
search=Wisconsin, DealsType= blank -> 9 rows, 8 unique PageUrl values
```

Current bucket evidence from `/tmp/transwestern_probe_20260612/feed_summary.json`:

| Bucket | Rows | Unique `PageUrl` | Bad `PageUrl` | Missing image | Missing property type | Zero price |
|---|---:|---:|---:|---:|---:|---:|
| all | 2,025 | 2,022 | 1 | 330 | 187 | 1,988 |
| `Sale` | 389 | 389 | 0 | 97 | 40 | 361 |
| `Lease` | 1,377 | 1,374 | 1 | 187 | 118 | 1,372 |
| `Sublease` | 129 | 129 | 0 | 24 | 7 | 129 |
| `Sale or Lease` | 130 | 130 | 0 | 22 | 22 | 126 |

Duplicate `PageUrl` values in the all-feed were exact duplicate rows:
`220-s-sylvania-ave-`, `building-iv-00`, and `wisconsin-place-00`, each with
count 2. The only bad `PageUrl` was `-` for `LaCenterra at Cinco Ranch - Phase
III`, so a production collector should skip rows with empty or `-` `PageUrl`
until a validated fallback detail URL exists.

The `/properties` HTML still shows the browser payload as:

```text
$.ajax({
  type: "POST",
  url: window.location.href,
  data: "&call=ajax&search=" + $("#tbxSearch").val() + ...
})
```

The same parameters remain safe to call as GET. The visible deal values are
`Lease`, `Sale`, `Sale or Lease`, and `Sublease`. Visible top-level property
type values include `100000000` Healthcare, `6` Hospitality, `2` Industrial,
`4` Land, `100000114` Life Sciences, `5` Multifamily, `1` Office,
`7` Other/Special Purpose, and `3` Retail. Do not need property-type filters for
the first collector because deal buckets already return the full current count.

### Endpoint And Path Evidence

- `https://transwestern.com/robots.txt` is public and points to
  `https://transwestern.com/sitemap.xml`.
- `https://transwestern.com/sitemap.xml` links to
  `https://transwestern.com/sitemap.aspx?xml=properties`.
- `sitemap.aspx?xml=properties` returned 2,025 property URLs on 2026-06-12.
- `/properties` HTML showed `Showing 2025 of 2025 Properties`, matching the
  sitemap count.
- `/properties` HTML includes the AJAX filter payload at lines around
  `$.ajax({ type: "POST", url: window.location.href, data: ... })`, but the
  same params succeeded over GET.
- The all-feed GET JSON returned fields: `PropertyImage`, `PageUrl`,
  `BuildingName`, `FullAddress`, `City`, `State`, `ZipCode`, `Price`,
  `PropertySize`, `Latitude`, `Longitude`, `PropertyTypeName`, and `distance`.
- Feed counts by GET bucket:
  - all deals: 2,025
  - `Sale`: 389
  - `Lease`: 1,377
  - `Sale or Lease`: 130
  - `Sublease`: 129
- Feed rows had 2,025 total rows, 2,022 unique `PageUrl` values, and one
  `PageUrl` value of `-`, which should be skipped or given a conservative
  fallback only after detail validation.
- Sample detail pages exposed title, address, description, property facts,
  coordinates in page JavaScript, broker cards, phone numbers, profile links,
  vCard URLs, image URLs, flyer PDFs, and floor-plan PDFs.
- Detail URL shape is deterministic for valid feed rows:
  `https://transwestern.com/property/${PageUrl}`.
- Local Firecrawl succeeded on
  `https://transwestern.com/property/1800-west-loop-south` with raw HTML,
  markdown, and links. The scrape result had `rawHtmlLen=69152`,
  `markdownLen=11564`, and `linksLen=18`.
- A fresh Firecrawl detail scrape of
  `https://transwestern.com/property/1025-w-national-avenue` returned
  `rawHtmlLen=72027`, `linksLen=11`, one PDF link, broker profile links, `tel:`
  links, and three vCard links.

### Field Shape And Collector Vocabulary

Feed rows expose these keys:

```text
PropertyImage, PageUrl, BuildingName, FullAddress, City, State, ZipCode, Price,
PropertySize, Latitude, Longitude, PropertyTypeName, distance
```

Collector mapping:

| Feed or detail field | Collector field |
|---|---|
| `PageUrl` | `id`, after duplicate handling, and `url` via `https://transwestern.com/property/${PageUrl}` |
| `BuildingName` | `name` |
| `PropertyTypeName` | `assetType` |
| `FullAddress` | `street` |
| `City` | `city` |
| `State` | `state` |
| `ZipCode` | `postalCode` |
| `Latitude`, `Longitude` | numeric `latitude`, `longitude` |
| `PropertySize` | `buildingSizeSqft` and `sizeText` such as `25,000 SF` |
| `Price` | keep in `rawData`; only promote to `salePriceUsd` when a nonzero numeric value is present |
| requested bucket | `transactionType` and existing `transactionMode` wrapper |
| `PropertyImage` | first `photos` entry when present |
| detail property facts | `rawData.transwesternFacts` plus optional richer size or class fields |
| detail availability table | `rawData.availability`, with row-level suite, sqft, rate, type, max contiguous, lease type, and CAM charges |
| detail `#tblAttachments` and `download-flyer-btn` links | `brochures` entries, including direct PDFs and redirect flyer URLs |
| detail `.chocolat-image` links | additional `photos` entries |
| detail `.PropertyVcard .v-card` blocks | `contactsDetailed` with `name`, `title`, `office`, `phone`, `profileUrl`, `vcardUrl`, and `avatarUrl` |

Transaction mapping for `srcTranswestern(tx, max)`:

- `tx === "sale"` should fetch `DealsType=Sale` plus `DealsType=Sale%20or%20Lease`.
- `tx === "lease"` should fetch `DealsType=Lease`, `DealsType=Sublease`, and
  `DealsType=Sale%20or%20Lease`.
- For `Sale or Lease` rows, emit `transactionType: "Sale/Lease"` so
  `cre_ingest.py` maps the final row to `sale_or_lease`.
- Dedupe within each transaction pass by `PageUrl` plus address hash when
  duplicate rows are exact copies. If the same `PageUrl` later appears with
  different facts, suffix the raw id with a short hash of address and
  `PropertySize`.

Detail enrichment selectors:

- Title: detail page `h1`.
- Facts: `li > b` label/value pairs under the property facts list.
- Coordinates: `myLatLng = { lat: <number>, lng: <number> }` script literal.
- Availability: `#tblAvailability` table.
- Documents: `#tblAttachments a.download-att-btn` and `.download-flyer-btn`.
- Photos: `.photos-list a.chocolat-image[href]`; fall back to feed
  `PropertyImage` when no gallery exists.
- Contacts: `.PropertyVcard .v-card`, with profile links such as
  `/joe.karmin` and vCard links such as
  `/vcard-generator?EntraPeopleID=<uuid>`.

Detail-page samples from `/tmp/transwestern_probe_20260612/detail_summary.json`:

| Detail page | Docs | Property photos | Contacts | Facts | Coordinates |
|---|---:|---:|---:|---:|---|
| `1025-w-national-avenue` | 1 PDF | 0 gallery photos, feed image available | 3 | 14 | yes |
| `1800-west-loop-south` | 0 | 11 gallery photos | 2 | 8 | yes |
| `444-w-interstate-road` | 1 PDF | 0 gallery photos, feed image available | 3 | 15 | yes |
| `marina-village-01` | 1 flyer redirect URL | 0 gallery photos | 2 | 7 | yes |

### Commands And Artifacts

Commands run from the repo root:

```bash
curl -sS https://transwestern.com/robots.txt
curl -sS https://transwestern.com/sitemap.xml
curl -sS -o /tmp/transwestern_sitemap_properties.xml \
  'https://transwestern.com/sitemap.aspx?xml=properties'
rg -o 'https://transwestern\.com/property/[^<]+' \
  /tmp/transwestern_sitemap_properties.xml | wc -l

curl -sS -o /tmp/transwestern_ajax_get_all.json \
  'https://transwestern.com/properties?call=ajax&search=&Latitude=&Longitude=&DealsType=&PropertyType=0&MetroName=&SubTypeIDs=&TenancyTypes=&CheckLeed=false&IsEnergyStar=false&MinPrice=&MaxPrice=&MinSize=&MaxSize=&SortType=asc&SortColumn=&class=&TotalLotSizeMin=&TotalLotSizeMax=&NoOfUnitsMin=&NoOfUnitsMax='
jq -r '"all_count=\(length)"' /tmp/transwestern_ajax_get_all.json

curl -sS -o /tmp/transwestern_ajax_get_lease.json \
  'https://transwestern.com/properties?call=ajax&search=&Latitude=&Longitude=&DealsType=Lease&PropertyType=0&MetroName=&SubTypeIDs=&TenancyTypes=&CheckLeed=false&IsEnergyStar=false&MinPrice=&MaxPrice=&MinSize=&MaxSize=&SortType=asc&SortColumn=&class=&TotalLotSizeMin=&TotalLotSizeMax=&NoOfUnitsMin=&NoOfUnitsMax='

curl -sS -o /tmp/transwestern_ajax_search_1800.json \
  'https://transwestern.com/properties?call=ajax&search=1800&Latitude=&Longitude=&DealsType=&PropertyType=0&MetroName=&SubTypeIDs=&TenancyTypes=&CheckLeed=false&IsEnergyStar=false&MinPrice=&MaxPrice=&MinSize=&MaxSize=&SortType=asc&SortColumn=&class=&TotalLotSizeMin=&TotalLotSizeMax=&NoOfUnitsMin=&NoOfUnitsMax='

python3 scripts/firecrawl-ops/firecrawl_request.py scrape \
  https://transwestern.com/property/1800-west-loop-south \
  --formats markdown,links,rawHtml \
  --out /tmp/transwestern_firecrawl_1800.json \
  --pretty --quiet --print-paths
```

Artifacts created during the read-only probe:

- `/tmp/transwestern_properties.html`
- `/tmp/transwestern_home.html`
- `/tmp/transwestern_sitemap_properties.xml`
- `/tmp/transwestern_ajax_get_all.json`
- `/tmp/transwestern_ajax_get_lease.json`
- `/tmp/transwestern_ajax_get_sale_or_lease.json`
- `/tmp/transwestern_ajax_get_sublease.json`
- `/tmp/transwestern_detail_1000_town_center.html`
- `/tmp/transwestern_detail_9219_viscount_row.html`
- `/tmp/transwestern_detail_1800_w_loop.html`
- `/tmp/transwestern_firecrawl_1800.json`
- `/tmp/transwestern_firecrawl_sitemap_properties.json`

Fresh refresh artifacts:

- `/tmp/transwestern_probe_20260612/properties.html`
- `/tmp/transwestern_probe_20260612/sitemap_properties.xml`
- `/tmp/transwestern_probe_20260612/ajax_all.json`
- `/tmp/transwestern_probe_20260612/ajax_sale.json`
- `/tmp/transwestern_probe_20260612/ajax_lease.json`
- `/tmp/transwestern_probe_20260612/ajax_sublease.json`
- `/tmp/transwestern_probe_20260612/ajax_sale_or_lease.json`
- `/tmp/transwestern_probe_20260612/ajax_search_1800.json`
- `/tmp/transwestern_probe_20260612/ajax_search_1025.json`
- `/tmp/transwestern_probe_20260612/ajax_search_wisconsin.json`
- `/tmp/transwestern_probe_20260612/detail_1025_w_national.html`
- `/tmp/transwestern_probe_20260612/detail_1800_w_loop.html`
- `/tmp/transwestern_probe_20260612/detail_444_w_interstate.html`
- `/tmp/transwestern_probe_20260612/detail_marina_village.html`
- `/tmp/transwestern_probe_20260612/firecrawl_detail_1025.json`
- `/tmp/transwestern_probe_20260612/feed_summary.json`
- `/tmp/transwestern_probe_20260612/detail_summary.json`

No binaries were downloaded and no Supabase ingest was run.

### Limitations

- The feed row `DealsType` field was `null` in sampled JSON rows, so the
  collector should tag transaction type from the requested bucket.
- Bucket overlap needs dedupe rules. Prefer `PageUrl` as the external id, with
  suffixes or merge logic for dual sale or lease status if the same `PageUrl`
  appears in more than one bucket.
- The all-feed response has one `PageUrl` equal to `-`; do not ingest that as a
  stable listing URL without extra validation.
- Feed `Price` is usually `0.0000`; sale prices and lease rates should come from
  the detail availability table when present. Preserve feed price in `rawData`.
- Some feed rows have no `PropertyImage`, and some detail pages have no gallery.
  The collector should accept missing photos.
- Some detail document links are `twurls.com` redirect URLs instead of direct
  PDFs. Store them as document URLs without dereferencing or downloading.
- Public detail pages expose vCard URLs, but not direct email addresses in the
  HTML samples. Store vCard and profile URLs as the durable contact enrichment.
- Detail pages are rich but heterogeneous. Some pages have full photos,
  availability tables, flyers, and contacts; others have sparse detail markup.
- The probe did not run a collector implementation, typecheck, ingestor
  dry-run, or sustained full scrape.
- The browser uses POST even though GET works. Keep the GET route documented
  and tested before relying on it for daily runs.

### Collector Patch Plan

1. Add `srcTranswestern(tx, max)` in `cre_collector/collect.ts`.
2. Add a small query helper that builds the canonical GET URL with
   `URLSearchParams`; do not hand-concatenate unescaped `Sale or Lease`.
3. For `tx === "sale"`, fetch `Sale` and `Sale or Lease`. For
   `tx === "lease"`, fetch `Lease`, `Sublease`, and `Sale or Lease`.
4. Use `scrapeJson` or a small same-file JSON fetch helper against the GET URL.
   The response may be served as `text/html`, so validate `Array.isArray(body)`.
5. Skip empty or `-` `PageUrl` rows. Dedupe exact duplicate rows by `PageUrl`.
6. Build stable listing URLs as `https://transwestern.com/property/${PageUrl}`.
7. Normalize feed fields into `id`, `name`, `assetType`, `street`, `city`,
   `state`, `postalCode`, `latitude`, `longitude`, `buildingSizeSqft`,
   `sizeText`, `photos`, `url`, and `rawData`.
8. Set `transactionType` from the bucket: `Sale`, `Lease`, `Sublease`, or
   `Sale/Lease`. The collector wrapper will also add `transactionMode`.
9. Enrich detail pages with raw HTML parsing for description, facts,
   availability tables, broker cards, profile links, vCard URLs, flyer URLs,
   direct PDF URLs, photos, and map coordinates. Record detail errors on the
   listing without failing the whole source.
10. Store document and image URLs only. Do not download or upload PDFs, vCards,
    or image binaries.
11. Add `transwestern` to `cre_ingest.py` `SOURCE_TO_BROKERAGE`; the mapping
    was absent in the 2026-06-12 probe. Add the `transwestern` brokerage seed
    to `../sql/001_cre_brokerages.sql` as well, since `rg transwestern` found no
    current seed entry.
12. Add `transwestern` to the production source status only after:
   - `npm run typecheck`
   - a small `TRANSWESTERN_QUERY=1800` or equivalent query-gated
     `npx tsx collect.ts --source=transwestern --transaction=both --max-items=6`
     probe, if the main patch adds a temporary query hook
   - `python3 cre_ingest.py --in <probe> --dry-run --keep-artifacts <dir>`
   - inspection that staged child rows contain URLs only.

Ready-to-implement `srcTranswestern` checklist:

- [ ] Build `transwesternFeedUrl({ dealsType, search })` with the exact params
  above.
- [ ] Fetch and parse bucket arrays over GET.
- [ ] Respect `max` after dedupe and before expensive detail enrichment.
- [ ] Use `PageUrl` for `id`; suffix with a short hash only if a future
  non-identical duplicate appears.
- [ ] Skip `PageUrl` `-`.
- [ ] Emit `photos` with feed image first, then detail gallery images deduped.
- [ ] Emit `brochures` from direct PDF links and flyer redirect URLs.
- [ ] Emit `contactsDetailed` from `.PropertyVcard .v-card`.
- [ ] Store facts and availability rows in `rawData`.
- [ ] Preserve the original feed row in `rawData.feed`.
- [ ] Keep detail failures nonfatal.
- [ ] Verify no binary payloads appear in collector output or dry-run SQL.

Exact main-thread code patch plan:

1. In `collect.ts`, add Transwestern helpers near the other source adapters:
   `TRANSWESTERN_BASE_PARAMS`, `transwesternFeedUrl`, `fetchTranswesternBucket`,
   `parseTranswesternFacts`, `parseTranswesternAvailability`,
   `parseTranswesternDocuments`, `parseTranswesternPhotos`, and
   `parseTranswesternContacts`.
2. Implement `srcTranswestern(tx, max)` using the bucket plan above and
   `pmap(..., CONCURRENCY, ...)` for detail enrichment.
3. In `runSource`, replace the unsupported skip with `case "transwestern":
   return srcTranswestern(tx, max);`.
4. Remove or update `UNSUPPORTED.transwestern`.
5. In `cre_ingest.py`, add `"transwestern": ("transwestern", "")` to
   `SOURCE_TO_BROKERAGE`, and add the matching seed row to
   `../sql/001_cre_brokerages.sql` before ingesting.
6. Update `cre_collector/CLAUDE.md`, `START_HERE.md`, and brokerage status only
   after the small probe, typecheck, and ingest dry-run pass.

### Unlock Conditions

Transwestern is unlocked for collector implementation now. It should remain
out of live Supabase ingest until the patch plan above passes on a small probe
and then a full bounded run. If the GET feed stops returning valid JSON, fall
back to sitemap plus detail-page GET parsing before considering any POST-body
path.

## 2026-06-12 Collector Implementation Note

Status: implemented in `scripts/firecrawl-ops/cre_collector/collect.ts` and
probe-proven, but not yet full-run or live-ingested.

What changed:

- `collect.ts` now includes `srcTranswestern(tx, max)`.
- The adapter uses the public `/properties?call=ajax` GET path, not the
  browser POST body.
- Sale collection fetches `DealsType=Sale` plus `DealsType=Sale or Lease`.
- Lease collection fetches `DealsType=Lease`, `DealsType=Sublease`, plus
  `DealsType=Sale or Lease`.
- Detail enrichment scrapes each deterministic `/property/{PageUrl}` page with
  raw HTML, markdown, and links.
- Detail parsing emits `contactsDetailed` with broker profile URLs, avatar
  URLs, and vCard URLs; `brochures` with direct PDF or flyer URLs; `photos`
  with feed and gallery image URLs; and raw `transwesternFacts` plus
  `availability` rows.
- Rows with empty or `-` `PageUrl` are skipped rather than assigned unstable
  synthetic URLs.
- `cre_ingest.py` now maps `transwestern` to the `transwestern` brokerage slug.
- `sql/001_cre_brokerages.sql` now seeds the Transwestern brokerage record.

Verification run from `scripts/firecrawl-ops/cre_collector`:

```bash
npm run typecheck
python3 -m py_compile cre_ingest.py
npx tsx collect.ts --source=transwestern --transaction=both --max-items=4 --concurrency=2 --out=/tmp/transwestern_collector_probe.json
python3 cre_ingest.py --in /tmp/transwestern_collector_probe.json --dry-run --keep-artifacts /tmp/transwestern_collector_ingest_check
```

Probe result:

- 8 listings collected, 4 sale-side and 4 lease-side.
- Feed buckets returned live rows: `Sale=389`, `Sale or Lease=130`,
  `Lease=1377`, `Sublease=129`.
- Detail enrichment completed for all 8 sampled listings with `detailErrors=0`.
- Extracted child URLs from the 8-row sample:
  - 22 document URLs.
  - 67 image URLs.
  - 23 detailed contact rows.
  - 23 broker profile URLs.
  - 23 vCard URLs.
- Ingest dry-run staged 8 Transwestern listings and skipped 0 missing URLs.

Remaining proof before calling complete:

- Run full `transwestern` sale and lease collection with `--max-items=0`.
- Dry-run the full artifact and inspect staged rows, child URL counts,
  duplicates, bad URLs, missing titles, invalid states, impossible coordinates,
  and malformed numeric fields.
- Apply `sql/001_cre_brokerages.sql` to Supabase if the live database has not
  yet received the Transwestern seed row.
- Live ingest only after the full dry-run is clean.
