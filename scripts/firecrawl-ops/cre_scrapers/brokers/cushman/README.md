# Cushman & Wakefield Scraper Notes

Production bulk collection currently lives in `../../../../cre_collector/collect.ts` under source key `cushman-wakefield`. This legacy Python scraper remains useful for detail-page experiments and parser prototyping.

## Public Search API

- Endpoint: `https://www.cushmanwakefield.com/api/properties/search`
- Required query shape:
  - `rfkId=property_search`
  - `view=pins`
  - `site_country=US`
  - `listing_type=Buy` for sale, `listing_type=Lease` for lease
  - `language=en`
  - `limit=100`
  - `offset=<0,100,200,...>`
- Verified totals on 2026-06-12 full run: sale returned `2743`, lease returned
  `8575`.
- Direct non-browser `urllib` calls can receive Azure Application Gateway `403`; the local Firecrawl API can fetch the endpoint.

## Detail Pages

Detail URLs follow:

`/en/united-states/properties/for-sale/<type>/<state>/<city>/<slug>/<id>-s`

or:

`/en/united-states/properties/for-lease/<type>/<state>/<city>/<slug>/<id>-l`

Search API rows may use `sitecore-www.cushmanwakefield.com`; canonicalize those to `www.cushmanwakefield.com`.

## Rich Data

- JSON-LD often exposes `RealEstateListing`, `datePosted`, address, offer, and offered broker/person metadata.
- Visible markdown sections expose building size, lot size, sale price, rental price, year built or renovated, contact names, phone numbers, profile links, and VCard links.
- PDF document URLs can be present in raw HTML but absent from Firecrawl's extracted `links`. Always scan raw HTML for `assets.cushmanwakefield.com/-/pmedia/...pdf`.
- Property photos are also under `assets.cushmanwakefield.com/-/pmedia/<property-media-id>/...`. Related-listing cards may include other pmedia IDs, so prefer the pmedia IDs that contain PDFs, then fall back to the first property pmedia image group.
- Store document and image URLs only. Do not download or upload the actual PDFs or image binaries into Supabase storage during bulk collection.

## Contact Caveat

VCard URLs expose richer contact data in a browser session, but local Firecrawl and direct Python calls can fail on those endpoints. Store the VCard URL and visible contact fields during bulk runs; use a browser-backed enrichment pass only when email capture becomes necessary.

## 2026-06-12 Full Run Proof

Artifact: `cre_collector/out/cushman_full_2026-06-12_022841.json`.

Command:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=cushman-wakefield --transaction=both --max-items=0 --page-cap=400 --concurrency=6 --out=out/cushman_full_2026-06-12_022841.json
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --dry-run --keep-artifacts /tmp/cushman_full_2026-06-12_022841_ingest_check
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/cushman_full_2026-06-12_022841_mark_missing_live
```

Result:

- Runtime: 4:41:00.
- Collected rows: 11,318, with 2,743 sale and 8,575 lease.
- Detail enrichment: 18,343 document URLs, 24,278 image URLs, 21,110 detailed
  contacts, 21,110 profile URLs, 20,301 VCard URLs, and 0 detail errors.
- Dry-run staged 11,318 rows and skipped 0 missing URLs.
- Live ingest used source-scoped `--mark-missing`, soft-deleting 24 old shallow
  probe rows.
- Supabase proof: 11,318 active Cushman rows; 0 missing URLs, missing titles,
  missing raw data, duplicate external IDs, bad state codes, impossible
  coordinates, malformed guarded prices/cap rates, or child orphans.
