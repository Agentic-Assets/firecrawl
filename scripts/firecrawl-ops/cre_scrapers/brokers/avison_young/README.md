# Avison Young Scraper Notes

Production bulk collection uses the Avison Young SharpLaunch public feed in
`cre_collector/collect.ts`.

## Site Structure

- Public pages are driven by a Liferay-style app and hash-state navigation.
- Rendered sidebar/list results can be parsed, but the production collector no
  longer depends on that shallow rendered batch.
- Treat this Python scraper as a source lab for future detail-page enrichment.

## Current Behavior

The production collector fetches the full active SharpLaunch website feed,
filters US-compatible rows, partitions sale and lease client-side, and joins
team-member contacts. Full detail-enriched runs are supported when
`AVISON_YOUNG_DETAIL_LIMIT` is set; otherwise unlimited daily runs stay
SharpLaunch-only by default so the daily path remains cheap and stable.

## 2026-06-12 Deep Dive Notes

Status: production collector coverage has been upgraded from the rendered
sidebar batch to the full active SharpLaunch feed.

Best public path found:

```text
GET https://pse-api.sharplaunch.com/data?entity=website&status=active
Header: X-Api-Key: b9fda00f3d4d7f623665270841e32176
```

The public key is embedded on the Avison Young property page in the
`SharpLaunch.PSE.create(...)` call. The SharpLaunch API ignores normal page,
limit, offset, and transaction query params, so the reliable pattern is one
full active-feed GET plus client-side transaction partitioning.

Bounded probe evidence:

- Rendered collector probe: 22 rows.
- Direct SharpLaunch active API: 2,202 active rows.
- US-compatible active rows: 2,200.
- US-compatible sale-like rows: 769.
- US-compatible lease-like rows: 1,450.
- US-compatible sublease rows: 114.
- US-compatible lease plus sublease rows: 1,564.
- Dual sale and lease rows: 133.

Artifacts saved by the probe:

```text
/tmp/ay_properties.html
/tmp/sharplaunch_SDK.js
/tmp/sharplaunch_427.js
/tmp/ay_config.json
/tmp/ay_data_active_only.json
/tmp/ay_data_active_escrow.json
/tmp/ay_team.json
/tmp/ay_detail_17341.html
/tmp/ay_rendered_probe.json
```

Detail-page proof on `4316 J M Turk Road` found public gallery image URLs, 3
public PDF document URLs, JSON-LD listing data, one broker in page HTML, and a
public team-member join with name, title, phone, email, company or location,
and `media_id`. No VCard URL or stable profile URL was verified on that sample.

Implemented collector behavior:

1. Parses the public key from the Avison Young property page, with the observed
   key above as a documented fallback.
2. Fetches `data?entity=website&status=active` and `data?entity=team_member`.
3. Filters to US-compatible rows and partitions sale, lease, and sublease
   client-side.
4. Uses `row.id` as the stable external id.
5. Preserves raw SharpLaunch subtypes and raw row payloads.
6. Joins `team_member_ids` into `contactsDetailed` with public name, title,
   email, phone, company or location, and constructible CDN avatar URL.
7. Stores only public source URLs, SharpLaunch URLs, and CDN image/avatar URLs.
8. For bounded runs, fetches public SharpLaunch/Avison detail pages for
   selected rows and extracts URL-only PDFs, richer public image URLs, JSON-LD,
   broker profile URLs, and visible VCard-like URLs.

Detail enrichment:

- Finite `--max-items=N` runs enrich the selected rows.
- Unlimited `--max-items=0` full-feed runs remain SharpLaunch-only unless
  `AVISON_YOUNG_DETAIL_LIMIT` is set.
- `AVISON_YOUNG_DETAIL_LIMIT=N` enriches at most `N` selected rows.
- `AVISON_YOUNG_DETAIL_LIMIT=0` forces detail enrichment off.
- `AVISON_YOUNG_DETAIL_CONCURRENCY` is capped by the collector
  `--concurrency` setting.
- Keep this bounded unless a full-detail runtime is deliberately scheduled.

## 2026-06-12 Full Run And Live Ingest

The SharpLaunch adapter was run as a full source-specific collection and
live-ingested additively.

Commands:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=avison-young --transaction=both --max-items=0 --concurrency=4 --out=out/avison_full_2026-06-12_043342.json
python3 cre_ingest.py --in out/avison_full_2026-06-12_043342.json --dry-run --keep-artifacts /tmp/avison_full_2026-06-12_043342_ingest_check
python3 cre_ingest.py --in out/avison_full_2026-06-12_043342.json --keep-artifacts /tmp/avison_full_2026-06-12_043342_live_ingest
```

Results:

- Artifact: `out/avison_full_2026-06-12_043342.json`, 6.4 MB.
- Log: `out/avison_full_2026-06-12_043342.log`.
- Collected raw rows: 2,333, including 769 sale-bucket rows and 1,564 lease-bucket rows.
- Unique staged rows: 2,200, because 133 dual sale/lease SharpLaunch rows merge into `sale_or_lease`.
- Brokers: 528 unique run-level broker records.
- Artifact coverage: 2,318 image URLs, 4,376 detailed contact rows, 0 document rows, and 0 detail errors.
- Dry-run staged 2,200 rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.

Supabase proof:

- Active Avison Young rows after ingest: 2,200.
- Transaction split: 636 sale, 1,431 lease, and 133 `sale_or_lease`.
- Latest-batch quality checks: 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad state codes, 0 impossible coordinates, 0 bad cap rates, 4,125 contact child rows, 2,186 image child rows, and 0 orphan contact/image rows.

This section is the SharpLaunch-only baseline. A later full detail-enriched run
was live-ingested additively from `out/avison_full_detail_2026-06-12.json`; see
the section below for current coverage.

## 2026-06-12 Full Detail-Enriched Run And Live Ingest

Commands:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
AVISON_YOUNG_DETAIL_LIMIT=2200 AVISON_YOUNG_DETAIL_CONCURRENCY=4 \
  npx tsx collect.ts --source=avison-young --transaction=both \
  --max-items=0 --concurrency=4 \
  --out=out/avison_full_detail_2026-06-12.json
python3 cre_ingest.py --in out/avison_full_detail_2026-06-12.json --dry-run --keep-artifacts /tmp/avison_full_detail_ingest_check
python3 cre_ingest.py --in out/avison_full_detail_2026-06-12.json --keep-artifacts /tmp/avison_full_detail_live_ingest
python3 cre_validate.py --format json
```

Results:

- Artifact: `out/avison_full_detail_2026-06-12.json`, 15.0 MB.
- Log: `out/avison_full_detail_2026-06-12.log`.
- Runtime: `2026-06-12T23:47:23.095Z` to `2026-06-13T00:35:38.996Z`.
- Collected raw rows: 2,332, including 769 sale-bucket rows and 1,563
  lease-bucket rows.
- Unique staged rows: 2,199.
- Artifact coverage: 2,721 document URLs, 33,945 image URLs, detail metadata
  on all rows, and 0 artifact-level detail errors.
- Photo leak check: 0 listing photo URLs matching `150x150`, `ay_logo`,
  `sharplaunch_header`, or `/media/`.
- Dry-run staged 2,199 rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.

Supabase proof:

- Active Avison Young rows after ingest: 2,201.
- Transaction split: 636 sale, 1,432 lease, and 133 `sale_or_lease`.
- Child rows: 4,128 contacts, 2,571 documents, and 31,570 images.
- Latest-batch quality checks: no missing state/title/coordinate flags; 2
  sale-PSF flags and 4 duplicate source URL groups remain as known data-quality
  patterns.
- VCard URLs remain absent from the public path, and broker profile URLs are
  sparse rather than guaranteed.

Current status: complete for the public SharpLaunch feed plus publicly
accessible detail-page fields.

## 2026-06-12 Bounded Detail Enrichment Proof

Commands:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=avison-young --transaction=both --max-items=2 --concurrency=2 --out=/tmp/avison_young_detail_probe_after_ingest_filter_2026-06-12.json
python3 cre_ingest.py --in /tmp/avison_young_detail_probe_after_ingest_filter_2026-06-12.json --dry-run --keep-artifacts /tmp/avison_young_detail_probe_after_ingest_filter_ingest
npm run typecheck
```

Result:

- 4 listings, 2 sale and 2 lease sample rows.
- 6 public PDF document URLs.
- 36 public image URLs.
- 5 contact rows.
- 1 broker profile URL.
- 0 VCard URLs in the sampled pages.
- 4 rows with JSON-LD captured.
- 0 detail errors.
- Dry-run ingest staged 4 rows and skipped 0 missing URLs.

Limits:

- The detail pass is public but request-heavy. Keep daily full-feed runs
  SharpLaunch-only unless `AVISON_YOUNG_DETAIL_LIMIT` is explicitly set.
- VCard URLs remain unproven in the checked samples.
- A later full detail-enriched run used `AVISON_YOUNG_DETAIL_LIMIT=2200`,
  staged 2,199 unique rows, and was live-ingested additively.
