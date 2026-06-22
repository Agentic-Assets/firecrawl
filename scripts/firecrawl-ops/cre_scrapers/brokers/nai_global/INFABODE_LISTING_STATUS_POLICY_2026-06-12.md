# NAI Global Infabode Listing Status Policy - 2026-06-12

Scope: NAI Global only, focused on the Infabode public feed and
`listingStatus` semantics. No Supabase ingest was performed. No documents,
images, or binaries were downloaded.

## Question

The public Infabode feed behind the NAI widget can page far beyond the recent
cards. An unbounded collector run from 2026-06-12 produced 13,597 NAI rows with
`publishedAt` dates from 2021 through 2026. This note answers whether those old
rows are active listings or public historical rows, and what collector policy is
safe for EQUIRE live listing ingestion.

## Public Surfaces Checked

- Widget page: `https://ab.infabode.com/nai-global/listings3`
- Feed endpoint: `POST https://infabode.com/public_api`
- Detail endpoint: `POST https://infabode.com/graphql`, query
  `publicPost(id: Int!)`
- Original NAI URLs from `publicPost.urlOriginal`, such as
  `https://www.naiglobal.com/listings/?propertyId=1010861-sale`

## Status Vocabulary

GraphQL introspection shows the public `ListingStatus` enum supports:

- `FOR_SALE_OFF_MARKET`
- `FOR_SALE_ON_MARKET`
- `SOLD`
- `UNDER_OFFER`
- `UNKNOWN`
- `WITHDRAWN_UNSOLD`

Observed in the 2026-06-12 unbounded NAI artifact:

| Status | Count |
|---|---:|
| `UNKNOWN` | 13,293 |
| `FOR_SALE_ON_MARKET` | 241 |
| null public detail/status | 53 |
| `SOLD` | 7 |
| `UNDER_OFFER` | 3 |

The unbounded artifact did not observe `FOR_SALE_OFF_MARKET` or
`WITHDRAWN_UNSOLD`, but the schema can return them.

## Active Filter Finding

The public widget feed does not expose a server-side active or on-market
filter.

`PostFilter` introspection on the public API returned fields for content type,
source, location, title/search, published date, and size. It did not return a
`listingStatus`, `listingStatuses`, `listing_status`, `listing_statuses`, or
`status` field. Targeted filter probes with those names returned GraphQL errors
like `Unknown field`.

The widget's default filter is therefore a source and content-type filter:

- `content_types_ids` or `contentTypesIds`: sale type `4`, lease type `10`
- `sourcesIds`: NAI member organization IDs
- no active/on-market status condition

`publishedAt` can bound recency, but it is not an active-listing signal.

## Old Rows

The default public feed paginates old rows, not only current inventory.
Targeted offsets returned:

| Offset | Sample date range |
|---:|---|
| 0 | 2026-06-05 |
| 540 | 2026-01-12 |
| 3,000 | 2025-03-05 |
| 6,000 | 2024-06-03 |
| 9,000 | 2023-11-01 |
| 12,000 | 2022-07-28 |
| 13,590 | 2021-08-11 |

The old records are still publicly reachable through Infabode `publicPost`, and
their original NAI `urlOriginal` values resolve to the NAI listings shell.
That does not make them active listings. Their status profile is mostly
`UNKNOWN`, with explicit `SOLD` and `UNDER_OFFER` rows appearing in the older
feed. This is best treated as a public mixed/historical listing feed, not a
clean current-inventory feed.

Sample detail checks:

| Infabode ID | Published | Status | Note |
|---:|---|---|---|
| 1602673 | 2026-06-05 | `FOR_SALE_ON_MARKET` | current sale sample |
| 1602675 | 2026-06-05 | `FOR_SALE_ON_MARKET` | current lease sample, status enum is sale-oriented |
| 1469545 | 2025-12-19 | `UNKNOWN` | public but not active-proven |
| 1223241 | 2024-12-13 | `UNDER_OFFER` | public but not active |
| 640406 | 2022-12-22 | `UNKNOWN` | public but not active-proven |
| 433673 | 2021-12-24 | `UNKNOWN` | public but not active-proven |

## Collector And Ingest Policy

For EQUIRE live listing ingestion, only `publicPost.listingStatus` containing
`FOR_SALE_ON_MARKET` should be considered active/on-market.

Do not live-ingest the unbounded NAI feed as active rows. The current ingestor
sets every staged listing to `status = 'active'`, so passing all NAI rows
through it would make thousands of public historical or ambiguous rows look
active in `credeals`.

Recommended policy:

1. Keep the public feed and `publicPost` detail path as the collection surface.
2. Fetch detail before deciding active eligibility, because the feed rows do
   not include `listingStatus`.
3. Live-ingest only rows whose detail status includes `FOR_SALE_ON_MARKET`.
4. Exclude `SOLD`, `UNDER_OFFER`, `FOR_SALE_OFF_MARKET`,
   `WITHDRAWN_UNSOLD`, `UNKNOWN`, null detail rows, and detail failures from
   active ingestion.
5. If historical NAI rows are useful, save them to a separate audit/archive
   artifact or future non-active table, not to the live active listing surface.
6. Preserve `listingStatus`, `publishedAt`, `urlOriginal`, and raw
   `publicPost` in `raw_data` for every retained row.
7. Do not use NAI for `--mark-missing` reconciliation until the collector and
   ingestor both enforce the active-status filter.

This policy is conservative. It sacrifices recall for live-current precision,
which is the safer choice for a deal intelligence board that treats
`cre_listings.status = 'active'` as current inventory.

## Commands Run

From `/Users/caymanseagraves/Github/agentic-assets/firecrawl`:

```bash
sed -n '1,320p' CLAUDE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/START_HERE.md
sed -n '1,280p' scripts/firecrawl-ops/cre_collector/CLAUDE.md
sed -n '1,320p' scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md
sed -n '1,360p' scripts/firecrawl-ops/cre_collector/archive/HANDOFF_LOG_2026-06-11.md
sed -n '1,260p' scripts/firecrawl-ops/cre_scrapers/CLAUDE.md
sed -n '1,700p' scripts/firecrawl-ops/cre_scrapers/brokers/nai_global/README.md
rg -n "Nai|nai-global|Infabode|infabode|listingStatus|NAI_" \
  scripts/firecrawl-ops/cre_collector/collect.ts \
  scripts/firecrawl-ops/cre_collector/cre_ingest.py \
  scripts/firecrawl-ops/cre_scrapers/brokers/nai_global -S
python3 - <<'PY'
# inspected existing NAI artifacts under scripts/firecrawl-ops/cre_collector/out
PY
python3 - <<'PY'
# wrote bounded public API/status probe artifacts under /tmp/nai_status_probe_2026-06-12
PY
python3 - <<'PY'
# checked small original NAI urlOriginal samples and saved shells under /tmp/nai_status_probe_2026-06-12
PY
```

Evidence artifacts from the bounded public probes were saved under:

```text
/tmp/nai_status_probe_2026-06-12/
```

