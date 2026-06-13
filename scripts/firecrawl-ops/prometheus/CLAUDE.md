# CLAUDE.md - prometheus/

Reference implementation from Firecrawl's Prometheus product, a cloud-based
CRE data collector originally written for CBRE's US commercial for-sale inventory.

## Files

| File | Description |
|------|-------------|
| `script.ts` | Original TypeScript collector using the cloud Firecrawl SDK |
| `data.json` | Pre-collected dataset: 5,877 CBRE US for-sale listings (11MB, collected 2026-06-11) |
| `README.md` | Original Prometheus README |

**These files are reference material. Do not modify them.**

## Key discovery: CBRE internal listings API

The script does NOT scrape the CBRE website. It hits CBRE's internal JSON API:

```
GET https://www.cbre.com/listings-api/propertylistings/query
    ?site=us-comm&Common.Aspects=isSale&PageSize=200&Page=1
```

Response shape:
```json
{ "DocumentCount": 5877, "Documents": [[{...listing fields...}]] }
```

This API is still behind Cloudflare (403 on direct curl). You must route it
through local Firecrawl with stealth proxy and `formats: ["rawHtml"]`.
The rawHtml contains the raw JSON body. Parse it directly, no HTML stripping needed.

Verified working locally:
```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.cbre.com/listings-api/propertylistings/query?site=us-comm&Common.Aspects=isSale&PageSize=200&Page=1",
    "formats": ["rawHtml"],
    "proxy": "stealth",
    "waitFor": 4000,
    "timeout": 60000
  }' | python3 -c "
import sys, json
d = json.load(sys.stdin)
html = d['data']['rawHtml']
parsed = json.loads(html[html.find('{'):html.rfind('}')+1])
print('DocumentCount:', parsed['DocumentCount'])
"
# -> DocumentCount: 5877
```

Note `waitFor: 4000`: the API endpoint renders much faster than the SPA detail pages.

## Local production adaptation

The local production implementation now lives in `../cre_collector/collect.ts`.
It uses the same CBRE internal JSON API through local Firecrawl stealth and
collects both sale and lease inventory. Uploads go through
`../cre_collector/cre_ingest.py`, which stages SQL and runs `psql` against the
`credeals` schema.

You can still inspect the reference `data.json` manually:
```python
import json
with open('prometheus/data.json') as f:
    data = json.load(f)
listings = data['listings']    # 5,877 normalized listing dicts
brokers  = data['brokers']     # deduplicated broker records
asset_base = data['assetBaseUrl']   # https://www.cbre.com/resources/fileassets/
# photo full URL = asset_base + listing['id'] + '/' + photo_path
```

## Field mapping: prometheus -> cre_listings

| prometheus field | cre_listings column | Notes |
|-----------------|---------------------|-------|
| `id` | `external_id` | e.g. `US-SMPL-160329` |
| `name` | `title` | |
| `street` | `address` | |
| `city` | `city` | |
| `state` | `state` | |
| `postalCode` | `zip` | |
| `latitude` | `lat` | |
| `longitude` | `lng` | |
| `totalAreaSqft` | `size_sf` | |
| `minAreaSqft` | `min_divisible_sf` | |
| `yearBuilt` | `year_built` | |
| `salePriceUsd` | `sale_price_usd` | null if `priceOnApplication=true` |
| `assetType` | `property_type` | map: "Retail" -> retail, "Land" -> land, etc. |
| `alsoForLease` | `transaction_type` | true -> sale_or_lease, false -> sale |
| `description` | `description` | |
| `highlights` | `highlights` (text[]) | |
| full record | `raw_data` (jsonb) | store the entire prometheus listing |
| `contacts[*]` | `cre_listing_contacts` | via brokers array lookup by index |
| `brochures[*]` | `cre_listing_documents` | `assetBaseUrl + id + '/' + path` |
| `photos[*]` | `cre_listing_images` | same asset URL construction |

## Similar APIs to investigate for other brokers

The Prometheus discovery confirms CBRE has an undocumented internal JSON API.
Other large Next.js/React SPA brokerages may have similar patterns:
- JLL: current collector uses public search pages. A structured API would reduce scrape time.
- Colliers: SalesTracker investment-sale via public RCM GET (`colliers`), plus
  the full main site via the public XML sitemap (`colliers-main`, unblocked
  2026-06-13). The Coveo POST path is not needed.
- Cushman & Wakefield: complete; public `/api/properties/search` pagination with
  detail enrichment, full run, live ingest, and Supabase validation done.
- Transwestern: complete; public GET feed, full run, live ingest, source-scoped
  reconciliation, and Supabase validation done.

Finding these APIs eliminates Cloudflare bypass overhead and yields structured data
directly, which is far preferable to parsing markdown from rendered pages.
