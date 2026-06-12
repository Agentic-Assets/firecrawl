# NAI Global Scraper Notes

Production bulk collection now uses the public Infabode GraphQL feed in
`cre_collector/collect.ts`.

## Current Status Policy

Read `INFABODE_LISTING_STATUS_POLICY_2026-06-12.md` before running or
ingesting an unbounded NAI collection. The public Infabode feed pages back to
2021 and does not expose a server-side active/on-market filter in `PostFilter`.
The defensible live-ingest policy is to fetch `publicPost` detail first and
treat only `listingStatus` containing `FOR_SALE_ON_MARKET` as active current
inventory. Public rows with `UNKNOWN`, `SOLD`, `UNDER_OFFER`,
`FOR_SALE_OFF_MARKET`, `WITHDRAWN_UNSOLD`, null detail, or detail failures
should not be loaded into the active `credeals` listing surface.

## Site Structure

- Public listings are exposed through a widget-style page.
- Some cards do not expose stable per-listing detail URLs.
- Cookie or consent behavior can affect rendered extraction.

## Ingest Consequence

The old rendered-card collector used synthesized external IDs and retained the
shared widget URL as `source_url`. The current GraphQL collector uses stable
`infabode:<id>` external IDs and `https://infabode.com/services/listings/<id>`
detail URLs.

## 2026-06-12 Deep Dive Notes

Scope: bounded NAI Global probe for source key `nai-global`. No Supabase ingest
was performed. No PDF or image binaries were downloaded. Temporary artifacts
were written under `/tmp`.

### Findings

- The current collector limitation is real for rendered card HTML, but not for
  the underlying widget data. The rendered widget cards do not expose anchor
  tags or stable IDs in the HTML that Firecrawl returns, which explains the
  current synthesized `card:` IDs and shared widget `source_url`.
- The widget bundle exposes a public Apollo endpoint:
  `https://infabode.com/public_api`.
- The listing grid query is public without bearer auth:
  `posts(filter, offset, limit)`.
- The widget's default filter uses NAI US member organization IDs from the
  bundle, `content_types_ids: [4, 10]`, and offset pagination with `limit: 18`.
  `4` maps to `Sale Listings`; `10` maps to `Lease Listings`.
- Stable public listing IDs are available as numeric Infabode post IDs. Use
  external IDs like `infabode:<id>` instead of synthesized card hashes.
- Stable per-listing URLs are available. The widget click handler opens:
  `https://infabode.com/services/listings/<id>`.
- The feed API returns title, summary, published date, location path, content
  type, source organization ID/name/logo/banner, and one or more image URLs.
- Detail enrichment is public through a second endpoint:
  `https://infabode.com/graphql`, query `publicPost(id: Int!)`.
- `publicPost` returns richer fields: full HTML content, tags, currency, price,
  listing status, land size, total size, size ranges, content type, post image
  URLs, source social links, and location geometry coordinates.
- Authenticated `post(id)` is not usable without a token, but anonymous
  `publicPost(id)` is enough for URL-only EQUIRE ingestion.
- Detail page render also works through local Firecrawl for
  `https://infabode.com/services/listings/1602673`, but GraphQL is the safer
  bulk path.
- The probed listing had no PDF/document URL and no public contact email:
  `contactEmail`, `urlDocument`, and `documentPreview` were null. The source
  website was exposed via `urlOriginal` and `source.socialLinks[0]`.
- No broker contact names, phone numbers, VCards, or direct brochure PDFs were
  found in the tested public API/detail path. Capture source organization links
  and visible source social links, but do not claim contact coverage yet.

### Evidence

Commands run from repo root unless noted:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
curl -L -sS --max-time 30 -A 'Mozilla/5.0' \
  'https://ab.infabode.com/nai-global/listings3' \
  -o /tmp/nai_global_listings3.html
cd scripts/firecrawl-ops/cre_collector && \
  npx tsx collect.ts --source=nai-global --transaction=both \
  --max-items=4 --page-cap=2 --concurrency=1 \
  --out=/tmp/nai_global_collect_probe.json
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://ab.infabode.com/nai-global/listings3' \
  --formats rawHtml,markdown,links \
  --out /tmp/nai_global_firecrawl_rendered.json
curl -L -sS --max-time 30 -A 'Mozilla/5.0' \
  'https://ab.infabode.com/_next/static/chunks/app/%5Bcompany%5D/listings3/page-498777599795cd4c.js' \
  -o /tmp/nai_global_page_bundle.js
curl -L -sS --max-time 30 -A 'Mozilla/5.0' \
  'https://ab.infabode.com/_next/static/chunks/651-8e1bba421adf6ab0.js' \
  -o /tmp/nai_global_chunk651.js
curl -sS --max-time 30 'https://infabode.com/public_api' \
  -H 'content-type: application/json' \
  -H 'origin: https://ab.infabode.com' \
  -H 'referer: https://ab.infabode.com/nai-global/listings3' \
  --data-binary @/tmp/nai_graphql_all_offset_0.body.json \
  -o /tmp/nai_graphql_all_offset_0.json
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://infabode.com/services/listings/1602673' \
  --formats rawHtml,markdown,links \
  --out /tmp/nai_detail_1602673_firecrawl.json
curl -sS --max-time 30 'https://infabode.com/graphql' \
  -H 'content-type: application/json' \
  -H 'origin: https://infabode.com' \
  -H 'referer: https://infabode.com/services/listings/1602673' \
  --data-binary @/tmp/nai_public_post_1602673_graphql.body.json \
  -o /tmp/nai_public_post_1602673_graphql.json
```

Key bounded results:

- Current collector probe wrote 8 rows, 4 sale and 4 lease, to
  `/tmp/nai_global_collect_probe.json`.
- Direct page `curl` returned a 34 KB Next.js shell with skeleton cards, not
  usable listing IDs.
- Widget bundle found `posts(filter, offset, limit)` and click target
  `https://infabode.com/services/listings/<id>`.
- `https://infabode.com/graphql` and `https://api.infabode.com/graphql` return
  `400 No query document supplied` on empty GET, confirming the GraphQL surface.
- `https://infabode.com/graphql` rejects auth-only `post(id)` as unauthorized.
- `https://infabode.com/public_api` returns public feed rows without bearer
  auth.
- Offset proof against `public_api`:
  - offset `0`: post IDs `1602675`, `1602673`, `1602672`.
  - offset `18`: post IDs `1602591`, `1602590`, `1602589`.
  - offset `36`: post IDs `1602328`, `1602325`, `1602322`.
- Detail proof against `graphql publicPost`:
  - sale `1602673`: 2 images, coordinates, price `225000`, currency `POUND`,
    land size `1.2`, status `FOR_SALE_ON_MARKET`, source links, and
    `urlOriginal=https://www.naiglobal.com/listings/?propertyId=-ne-14th-st-ne-39th-ave-ocala-sale`.
  - lease `1602675`: 1 image, coordinates, price `1600`, size total `4999`,
    content type `Lease Listings`, source social links.
- Extra field probe for `1602673` returned `contactEmail=null`,
  `urlDocument=null`, and `documentPreview=null`.

### Status Recommendation

Upgrade NAI Global from `Partial, first rendered batch` to `Needs patch,
public GraphQL feed found`. After implementation and a bounded/full run, it
should likely become a complete public feed for the public Infabode-backed
inventory, with a documented contact/document limitation unless another public
field appears.

### Collector Patch Plan

1. Replace rendered-card parsing in `srcNaiGlobal` with a GraphQL feed client
   against `https://infabode.com/public_api`.
2. Extract or hard-code the current NAI US source IDs from the widget bundle.
   Prefer a checked-in constant with a note that it came from module `4373` in
   `/listings3` chunk `651-8e1bba421adf6ab0.js`, because scraping the bundle on
   every run adds fragility.
3. Page `posts(filter, offset, limit)` with `limit: 18`, incrementing offset
   until a short page is returned. Respect `max` and keep `--page-cap` as a
   guard for this source.
4. Split sale and lease by content type ID, `4` for sale and `10` for lease,
   rather than parsing visible card labels.
5. Use `id: "infabode:<post.id>"`, `url:
   "https://infabode.com/services/listings/<post.id>"`, and preserve
   `raw_data.feedRow`.
6. Enrich each row through `https://infabode.com/graphql` `publicPost(id)`.
   Store `raw_data.publicPost` and tolerate per-detail failures by retaining
   the feed row with `detailError`.
7. Map detail fields into collector output: full HTML content as description
   or raw field, tags as asset/type hints, `price`, `currency`, `landSize`,
   `sizeTotal`, `sizeRangeL/H`, `listingStatus`, location coordinates, source
   organization, source website/social links, and all `postImages` URLs.
8. Continue storing only URLs for documents/images. Do not download images or
   documents.
9. Leave contacts empty unless a later public field exposes real broker names,
   phone numbers, emails, profile URLs, or VCards.
10. Verification after patch: small probe `--max-items=6`, dry-run ingest with
    kept artifacts, then full `nai-global` sale+lease run before changing
    status files.

## 2026-06-12 Standalone GraphQL Probe

Artifact prefix: `/tmp/nai_global_graphql_probe_2026-06-12`.

Command run from repo root:

```bash
python3 /tmp/nai_global_graphql_probe_2026-06-12.py
```

Artifacts written:

- `/tmp/nai_global_graphql_probe_2026-06-12.py`
- `/tmp/nai_global_graphql_probe_2026-06-12.report.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.feed_offset_0.body.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.feed_offset_0.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.feed_offset_18.body.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.feed_offset_18.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.publicPost_sale_1602673.body.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.publicPost_sale_1602673.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.publicPost_lease_1602675.body.json`
- `/tmp/nai_global_graphql_probe_2026-06-12.publicPost_lease_1602675.json`

Probe result summary:

- Feed page `offset=0, limit=18` returned 18 stable Infabode IDs:
  `1602675`, `1602673`, `1602672`, `1602671`, `1602670`, `1602658`,
  `1602611`, `1602608`, `1602605`, `1602601`, `1602599`, `1602598`,
  `1602597`, `1602596`, `1602595`, `1602594`, `1602593`, `1602592`.
- Feed page `offset=18, limit=18` returned 18 stable Infabode IDs:
  `1602591`, `1602590`, `1602589`, `1602585`, `1602583`, `1602579`,
  `1602576`, `1602571`, `1602560`, `1602519`, `1602516`, `1602514`,
  `1602511`, `1602419`, `1602345`, `1602342`, `1602334`, `1602331`.
- In those 36 rows, content type `4` returned 20 sale rows and content type
  `10` returned 16 lease rows. No unknown content type appeared.
- Sale detail probe: `publicPost(1602673)`, `1.2 Ac Corner Lot - NE 14th
  Street`, 2 image URLs, coordinates, land size `1.2`, source organization
  `NAI Heritage`.
- Lease detail probe: `publicPost(1602675)`, `Hidden Lake Office Suites`, 1
  image URL, coordinates, `sizeTotal=4999`, source organization
  `NAI G2 Commercial`.
- Both public detail responses omit broker names, phones, emails, profile URLs,
  VCards, brochure URLs, document URLs, and document preview URLs. Source
  organization social links are available and should be stored only in
  `raw_data` unless the collector schema grows a source-link child table.
- The public API returns `currency: "POUND"` for the tested US records. Treat
  this as a raw provider value. Do not populate `salePriceUsd` from it unless a
  later proof confirms USD semantics.

### Exact Query Shapes

Feed endpoint:

```http
POST https://infabode.com/public_api
origin: https://ab.infabode.com
referer: https://ab.infabode.com/nai-global/listings3
content-type: application/json
```

Feed body shape:

```json
{
  "query": "query GET_LISTINGS_POSTS($filter: PostFilter, $offset: Int, $limit: Int) { posts(filter: $filter, offset: $offset, limit: $limit) { id title summary publishedAt locations { id path } contentType { id name } source { id name logoS3(format: LOGO_300X300) bannerS3 } postImages { id url } } }",
  "variables": {
    "offset": 0,
    "limit": 18,
    "filter": {
      "content_types_ids": [4, 10],
      "indSectorsIds": [],
      "sourcesIds": [99487, 99571, 99491, 99492, 84593, 99494, 99495, 84587, 99573, 161338, 84617, 268182, 84557, 99574, 99499, 268184, 99500, 85394, 99501, 99502, 99503, 99577, 84594, 209408, 99505, 99506, 77674, 99507, 99508, 85523, 99509, 85516, 99510, 77668, 99511, 99513, 99514, 99516, 99517, 99518, 99519, 84585, 92844, 99520, 99581, 99521, 99522, 84591, 99523, 77643, 99524, 99525, 77682, 85417, 99526, 77670, 99527, 99530, 99532, 200927, 99533, 99534, 87675, 194245, 99536, 99537, 87673, 84622, 99538, 99540, 210201, 194610, 99543, 77675, 86241, 87997, 149117, 234516, 99545, 99546, 92845, 99548, 99549, 99550, 99583, 182876, 99551, 99531, 99552, 84621, 99486, 99554, 99555, 99556, 83286, 294858, 268194, 99557, 92846, 77680, 99558, 99559, 99560, 268195, 99561, 99535, 99584, 99562, 99563, 109852, 99498, 99566, 99567, 99569, 99585, 92843],
      "locationsIds": [],
      "title": ""
    }
  }
}
```

Detail endpoint:

```http
POST https://infabode.com/graphql
origin: https://infabode.com
referer: https://infabode.com/services/listings/<id>
content-type: application/json
```

Detail body shape:

```json
{
  "query": "query publicPost($id: Int!) { publicPost(id: $id) { id title summary content tags currency listingStatus price landSize sizeTotal sizeRangeH sizeRangeL urlOriginal contactEmail urlDocument documentPreview contentType { id name } postImages { id url index } locations { id name geometry path } source { id socialLinks name bannerS3 logoS3(format: LOGO_100X100) } } }",
  "variables": {
    "id": 1602673
  }
}
```

## 2026-06-12 Collector Patch Verification

Scope: NAI Global only. No live Supabase ingest was performed, no
`--mark-missing` was used, and no PDF or image binaries were downloaded.

Commands run:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=nai-global --transaction=both --max-items=4 --out=/tmp/nai_before_probe.json
npm run typecheck
npx tsx collect.ts --source=nai-global --transaction=both --max-items=6 --out=/tmp/nai_after_probe.json
python3 cre_ingest.py --in /tmp/nai_after_probe.json --dry-run --keep-artifacts /tmp/nai_after_ingest_check
```

Results:

- Before patch: `/tmp/nai_before_probe.json` had 8 listings, 4 sale and 4
  lease, all with `card:` IDs, 1 shared source URL, 8 image URLs, 0 brochure
  URLs, 0 contact rows, 0 source website URLs, and 0 source social links.
- After patch: `/tmp/nai_after_probe.json` had 12 listings, 6 sale and 6
  lease, all with `infabode:` IDs, 12 unique detail URLs, 39 image URLs, 0
  brochure URLs, 0 contact rows, 12 source website URLs, 32 source social link
  URLs in raw data, and 0 detail errors.
- Dry-run ingest staged 12 usable rows and skipped 0 rows for missing URL.
  Generated SQL artifact: `/tmp/nai_after_ingest_check/ingest.sql` (99,994
  bytes).

Implemented behavior:

- Uses documented public `POST https://infabode.com/public_api` for
  `posts(filter, offset, limit)`.
- Uses documented public `POST https://infabode.com/graphql` for
  `publicPost(id)`.
- Keeps detail failures at listing level through `detailError`; a failed
  detail call does not fail the whole NAI source.
- Stores image and document children as URLs only. `urlDocument` and
  `documentPreview` remain empty in the verified probe, so no brochure rows
  were produced.
- Keeps broker/contact rows empty unless public `contactEmail` appears. The
  verified probe still exposed no broker names, phones, profile URLs, or
  VCards.

### Ready-To-Implement Mapper Checklist

> NOTE 2026-06-12: all items in this checklist were implemented and verified
> in the 2026-06-12 collector patch verification. The list is retained below
> as a mapping reference.

- Add constants near `srcNaiGlobal`: `NAI_PUBLIC_API_URL`,
  `NAI_PUBLIC_POST_URL`, `NAI_LISTING_URL_BASE`, `NAI_PAGE_SIZE=18`,
  `NAI_CONTENT_TYPE_BY_TX = { sale: 4, lease: 10 }`, and the 116
  `NAI_SOURCE_IDS` from `/tmp/nai_global_graphql_probe_2026-06-12.py`.
- Add a small JSON POST helper for Infabode GraphQL using `fetch` or Node's
  built-in `fetch`, with the exact `origin`, `referer`, `content-type`, and
  `user-agent` headers above. This source should not depend on local
  Firecrawl because the GraphQL API is directly public.
- Replace rendered-card scraping inside `srcNaiGlobal` with offset pagination:
  request `posts(filter, offset, limit)` at offsets `0, 18, 36, ...`, stop on
  a short page, empty page, `max` reached, or `PAGE_CAP` reached.
- For each feed row, classify by `row.contentType.id`: `4` is sale and `10` is
  lease. In the `srcNaiGlobal(tx, max)` pass, keep only rows matching `tx`.
- Use `id: "infabode:" + row.id` and
  `url: "https://infabode.com/services/listings/" + row.id`.
- Fetch `publicPost(id)` for each kept row with bounded concurrency. If detail
  fetch fails, retain the feed row and add `raw_data.detailError`.
- Map `title` to `name`; `summary` or text-stripped `content` to
  `description`; `contentType.name` and `tags` to `assetType` or
  `raw_data.tags`; comma-separated `locations[0].path` to `city`, `state`, and
  `country`; `locations[0].geometry.coordinates` to `longitude` and
  `latitude`; `postImages[].url` to `photos`.
- Map sale price only to `salePriceText` until currency semantics are proven.
  For lease, map provider price to `leaseRateText`. Do not set
  `salePriceUsd` from `currency: "POUND"` on US listings.
- Map `sizeTotal` to `buildingSizeSqft` and `sizeText` where numeric, and
  `landSize` to `lotSizeAcres` when present.
- Keep `brokerIds: []` unless a later public field exposes broker identity.
  Keep `brochures: []` unless a later public field exposes document URLs. Do
  not download images or documents.
- Store `raw_data.feedRow`, `raw_data.publicPost`, `raw_data.sourceOrganization`,
  `raw_data.listingStatus`, `raw_data.tags`, and the raw provider currency.
- Return method text like `Infabode public GraphQL feed plus publicPost detail
  enrichment, offset paginated`.
- After patching `collect.ts`, verify with:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=nai-global --transaction=both --max-items=6 --page-cap=2 --concurrency=2 --out=/tmp/nai_global_collect_graphql_probe.json
python3 cre_ingest.py --in /tmp/nai_global_collect_graphql_probe.json --dry-run --keep-artifacts /tmp/nai_global_ingest_graphql_probe
```
