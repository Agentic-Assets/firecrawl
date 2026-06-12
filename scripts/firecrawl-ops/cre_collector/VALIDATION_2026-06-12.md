# CRE Collector Supabase Validation - 2026-06-12

Validation time: 2026-06-12 local time.

## Verdict

The latest full collection was uploaded correctly for the rows it safely staged, and later source-specific ingests have completed several formerly partial sources. The system still does not contain every public listing from every target brokerage because some sources remain partial or blocked by source limits.

The latest all-source artifact staged 33,488 unique rows and the live Supabase rows touched by that artifact also total 33,488. Later source-specific ingests completed additional brokerages, including Lee & Associates. The original all-source run intentionally skipped `--mark-missing` because Lee & Associates failed at that time.

## Fresh Evidence

- Supabase project: `fhqycqubkkrdgzswccwd`, `supabase-agentic-assets-v2`, active healthy.
- Latest full artifact: `out/full_latest_2026-06-11_230423.json`.
- Artifact metadata: started `2026-06-12T04:04:23.566Z`, finished `2026-06-12T04:31:24.562Z`.
- Raw artifact rows: 35,510.
- Dry-run staged rows: 33,488, skipped for missing URL: 0.
- Latest live job rows: 10 brokerages, 35,510 discovered, 33,488 saved, 2 errors.
- Latest live touched rows by `scraped_at='2026-06-12 04:31:24.562+00'`: 33,488.
- Active rows in `credeals.cre_listings`: 34,218.
- Active rows in `credeals.v_cre_listings_full`: 34,218.
- Sale view rows: 10,932.
- Lease view rows: 25,531.
- Market summary rows: 9,883.

## Upload Reconciliation

Latest job saved rows matched latest touched rows for every uploaded brokerage:

| Brokerage | Discovered | Saved | Latest touched | Errors |
|---|---:|---:|---:|---:|
| Avison Young | 22 | 21 | 21 | 0 |
| CBRE plus Deal Flow | 20,705 | 19,044 | 19,044 | 0 |
| Cushman & Wakefield | 24 | 24 | 24 | 0 |
| JLL plus Investor | 4,728 | 4,593 | 4,593 | 0 |
| Lee & Associates | 0 | 0 | 0 | 2 |
| Marcus & Millichap | 12 | 12 | 12 | 0 |
| NAI Global | 30 | 19 | 19 | 0 |
| Newmark | 4,368 | 4,368 | 4,368 | 0 |
| Savills | 100 | 100 | 100 | 0 |
| SVN | 5,521 | 5,307 | 5,307 | 0 |

Deduping explains the gap between raw discovered rows and saved rows. Examples: CBRE Deal Flow folds into CBRE, JLL Investor folds into JLL, sale and lease passes can merge to `sale_or_lease`, and Buildout dual-mode rows merge by property id.

## Quality Checks

Latest touched rows had:

- 0 bad URLs.
- 0 missing titles.
- 0 bad or missing transaction types.
- 0 bad state codes.
- 0 invalid latitude or longitude values.
- 0 malformed sale prices under the ingestor guard.
- 0 malformed cap rates under the ingestor guard.
- 0 malformed lease rates under the ingestor guard.
- 0 missing `raw_data`.
- 0 duplicate `(brokerage_id, external_id)` groups.
- 0 orphan contacts, documents, or images.

One duplicate `source_url` group exists by design: NAI Global cards do not expose per-card links, so the collector uses the shared widget URL and synthesized card ids.

## Known Gaps

- Post-validation live ingest: Cushman & Wakefield was upgraded from the shallow rendered Coveo card path to the public `/api/properties/search` path with detail-page enrichment and live-ingested from `out/cushman_full_2026-06-12_022841.json`. The artifact collected 11,318 rows, 2,743 sale and 8,575 lease, with 0 detail errors and 0 skipped missing URLs. Source-scoped `--mark-missing` soft-deleted the 24 older Cushman probe rows. Live validation found 11,318 active Cushman rows, 18,343 document URL rows, 24,278 image URL rows, 21,110 contact rows, 21,110 profile URLs, 20,301 VCard URLs, and 0 missing URLs, titles, raw data, duplicate external IDs, bad states, impossible coordinates, malformed prices/cap rates, or child orphans.
- Post-validation live ingest: CBRE Deal Flow was upgraded from the old first-grid path to the public RCM ListingEngine endpoint and ingested additively from `out/cbre_dealflow_full_2026-06-12_041740.json`. The artifact staged 1,836 rows, 1,809 public sale cards and all 27 public lease cards, with 0 skipped missing URLs. RCM reported 2,042 sale rows, but public card pagination exposed 1,809 sale cards before returning 0 additional cards. Live Supabase now has 1,857 active Deal Flow-prefixed rows under brokerage slug `cbre`, including prior additive probe rows retained because `--mark-missing` was not used.
- Post-validation live ingest: Newmark no-state recovery was ingested additively from `out/newmark_full_2026-06-12_no_state_recovery.json`. The artifact staged 4,371 rows, 1,121 sale and 3,250 lease, with 0 skipped missing URLs. Latest-batch validation found 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad states, 0 bad coordinates, 0 bad cap rates, 4,303 image child rows, and 0 orphan images.
- Post-validation live ingest: Avison Young SharpLaunch full feed was ingested additively from `out/avison_full_2026-06-12_043342.json`. The artifact collected 2,333 raw rows and staged 2,200 unique rows after dual sale/lease merge, with 0 skipped missing URLs. Latest-batch validation found 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad states, 0 bad coordinates, 0 bad cap rates, 4,125 contact child rows, 2,186 image child rows, and 0 orphan contact/image rows.
- Superseded Lee note: Lee & Associates was not uploaded in the original
  all-source validation run because sustained Buildout paging failed near pages
  286 through 297. Later on 2026-06-12, durable page-cache/window assembly
  completed all pages and Lee was live-ingested with validation. Keep the older
  note as failure-mode evidence, but use the later Lee section below for current
  coverage.
- Post-validation live ingest: Colliers SalesTracker was ingested additively
  from `out/colliers_salestracker_full_2026-06-12_050241.json`. The artifact
  collected 1,300 public SalesTracker sale cards from RCM GET list/map endpoints
  and staged 1,172 unique rows after duplicate project IDs, with 0 skipped
  missing URLs. The artifact retained 486 card/map rows without public SLP
  detail links, captured 2,915 contact rows and 10,036 image URLs, and produced
  0 `detailError` rows. Live Supabase validation found 1,172 active Colliers
  rows, 2,733 contact child rows, 9,908 image child rows, 0 document rows, 0
  missing URLs, 0 missing titles, 0 missing raw data, 0 bad state codes, 0
  impossible coordinates, 0 duplicate external IDs, and 0 orphan
  contacts/documents/images. Sample `search_cre_listings('office', null, null,
  null, 'sale')` returned live Colliers rows. The main
  `www.colliers.com/en/properties` Coveo sale/lease search remains blocked.
- Post-validation live ingest: Transwestern was loaded from `out/transwestern_full_2026-06-12_121302_cleaned.json`. The full public GET/detail artifact collected 2,151 raw rows, which staged to 2,021 unique listings after 130 sale-or-lease duplicates merged. The cleaned artifact removed 2,151 footer/TREC/copyright descriptions and retained URL-only child assets. Live validation found 2,021 active rows, 389 sale, 1,502 lease, 130 sale_or_lease, 3,054 document URL rows, 4,838 image URL rows, 3,746 contact rows, 3,746 profile URLs, 3,746 VCard URLs, and 0 bad descriptions, bad asset URLs, missing URLs, missing titles, missing raw data, duplicate external IDs, bad states, impossible coordinates, malformed guarded prices/cap rates, or child orphans.
- Post-validation live ingest: Marcus & Millichap was loaded from
  `out/marcus_full_2026-06-12_130035.json` after adding a Marcus-only detail
  JSONL checkpoint beside the artifact. The full public ActivityId/map-detail
  artifact collected 3,124 sale rows and 0 lease rows, with 0 missing URLs, 0
  missing titles, 0 duplicate IDs, 0 final detail errors, 16,771 image URLs,
  7,915 visible contact/profile URL rows, and 3,124 gated deal-room URLs kept
  only in raw metadata. Dry-run staged all 3,124 rows and skipped 0 missing
  URLs. Source-scoped `--mark-missing` was dry-run and then applied only for
  `marcus-millichap`. Live validation found 3,124 active Marcus rows, all sale,
  16,771 image child rows, 7,915 contact child rows, 0 document rows, and 0 bad
  source URLs, missing titles, missing raw data, duplicate external IDs, bad
  states, impossible coordinates, malformed guarded prices/cap rates, bad child
  URLs, or child orphans. Search proof used the updated five-argument
  `credeals.search_cre_listings` signature and returned live Marcus rows.
- Post-validation live ingest: Lee & Associates was loaded from
  `out/lee_full_cache_2026-06-12_assembled.json` after adding durable Buildout
  page-cache/window controls. Cache-only fills produced contiguous pages 0
  through 332, with page 0 and page 332 both reporting `total=9975` and
  `limit=30`. Cache-only windows intentionally refused to write partial listing
  artifacts. The assembled artifact collected 9,975 raw Buildout rows, 3,447
  sale and 6,528 lease, with 0 missing URLs, 0 missing titles, 8,238 document
  URL rows, 9,975 image URLs, and 1,085 unique run-level brokers. Dry-run staged
  9,223 unique rows and skipped 0 missing URLs; the 752-row reduction is
  expected because the ingestor strips Buildout `propertyId` `-sale`/`-lease`
  suffixes and merges 744 sale+lease property pairs plus 8 exact duplicate
  rows. Source-scoped `--mark-missing` was dry-run and then applied only for
  `lee-associates`. Live validation found 9,223 active Lee rows, 2,611 sale,
  5,691 lease, 921 sale_or_lease, 9,062 image child rows, 7,681 document child
  rows, 9,223 contact child rows, and 0 bad source URLs, missing titles, missing
  raw data, duplicate external IDs, bad states, impossible coordinates,
  malformed guarded prices/cap rates, bad child URLs, or child orphans. Search
  proof used the updated five-argument `credeals.search_cre_listings` signature
  and returned live Lee rows.
- Post-validation live ingest: Newmark was refined and reloaded from
  `out/newmark_full_refined_2026-06-12.json`. The collector now retries the
  public Algolia credential bootstrap, uses direct public Algolia JSON for
  listing and People queries, infers `DC` for the three Washington DC no-state
  lease rows, preserves `rawNewmarkHit` and `newmarkBrokerProvenance`, and
  populates `contactsDetailed` from exact public People name matches. The full
  artifact collected 4,371 rows, 1,121 sale and 3,250 lease, with 0 missing
  URLs, 0 missing titles, 0 missing states, 3 DC-recovered rows, 3,961
  contact/profile rows, 3,910 contacts with phone, 4,303 image URLs, and 0
  document rows. Dry-run staged all 4,371 rows and skipped 0 missing URLs.
  Source-scoped `--mark-missing` was dry-run and then applied only for
  `newmark`, soft-deleting 715 old additive rows. Live validation found 4,371
  active Newmark rows, 1,121 sale, 3,250 lease, 4,303 image child rows, 3,961
  contact child rows/profile URLs, 0 document rows, and 0 bad source URLs,
  missing titles, missing raw data, missing states, invalid states, duplicate
  external IDs, impossible coordinates, malformed guarded prices/cap rates, bad
  image/profile URLs, or child orphans. Search proof used
  `credeals.search_cre_listings('Alvista', null, null, null, null)` and
  returned the live Newmark `Alvista Sterling Palms` row.
- 724 older active rows remain from earlier additive runs after Marcus
  source-scoped reconciliation: Newmark 715, CBRE 5, Savills 2, SVN 2. Do not
  treat active row count as a pure latest-run count until a clean reconciliation
  run marks missing rows.
- Some supported adapters are intentionally shallow: Avison Young and Savills
  have first-page, first-batch, or source-fit limitations documented in
  `CLAUDE.md`. Marcus & Millichap was removed from this shallow list for the
  public sale feed after the 2026-06-12 ActivityId expansion, full run,
  source-scoped reconciliation, and live validation; public lease remains
  blocked. Cushman was removed from this list after the 2026-06-12 API upgrade,
  full run, source-scoped reconciliation, and live validation. NAI Global was
  removed after the public Infabode GraphQL active-status filter was proven and
  live-ingested on 2026-06-12.
- Post-validation probe: Savills now has a defensible public U.S. commercial
  lease path from the server-rendered commercial lease page. The bounded probe
  collected 2 Chicago, IL retail lease rows with 4 public PDF document URLs, 24
  image URLs, and 2 contact rows. Dry-run ingest staged both rows and skipped 0
  missing URLs. No live ingest was run. Savills sale remains partial and not
  CRE-defensible because the current sale route is global/residential, while
  the corrected commercial sale route exposed only a Toronto, Canada object.

## Access Model

The collector-owned tables and views are service-role only. `anon` and `authenticated` have no grants on `cre_brokerages`, `cre_listings`, `cre_listing_contacts`, `cre_listing_documents`, `cre_listing_images`, `cre_scrape_jobs`, or the `v_cre_*` views. RLS is enabled on the base tables with no public policies.

## Commands Run

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
python3 -m py_compile cre_ingest.py
python3 cre_ingest.py --in out/full_latest_2026-06-11_230423.json --dry-run --keep-artifacts /tmp/cre_validate_20260612
npx tsx collect.ts --source=lee-associates --transaction=both --max-items=0 --page-cap=400 --concurrency=3 --out=out/lee_latest_2026-06-12_004010.json
```

The Lee command was piped through `tee`, so the shell process returned the `tee` exit code. The collector output itself contains the failure and no JSON artifact was produced.

## Next Fixes

1. Make Lee throttling-safe before any claim of all-source coverage. Candidate fixes: lower Buildout concurrency for Lee, add longer page-batch cooldowns, persist successful pages to a resumable cache, or collect Lee in smaller page windows.
2. Add a saved validation command that compares latest artifact staged rows to Supabase touched rows by brokerage.
3. After Lee is clean, run a full all-source collection and live ingest with mark-missing eligibility, then verify that stale active rows are gone or intentionally retained.
4. Implement Lee Buildout resumability/page-cache controls before another
   sustained Lee full run. Treat main Colliers Coveo sale/lease coverage as
   integration backlog until a permitted non-POST path exists.

## 2026-06-12 CBRE Deal Flow Full Run And Live Ingest

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=cbre-dealflow --transaction=both --max-items=0 --concurrency=4 --out=out/cbre_dealflow_full_2026-06-12_041740.json
python3 cre_ingest.py --in out/cbre_dealflow_full_2026-06-12_041740.json --dry-run --keep-artifacts /tmp/cbre_dealflow_full_2026-06-12_041740_ingest_check
python3 cre_ingest.py --in out/cbre_dealflow_full_2026-06-12_041740.json --keep-artifacts /tmp/cbre_dealflow_full_2026-06-12_041740_live_ingest
```

Collector result:

- Artifact: `out/cbre_dealflow_full_2026-06-12_041740.json`.
- Log: `out/cbre_dealflow_full_2026-06-12_041740.log`.
- Runtime: 5:58.
- Collected rows: 1,836, including 1,809 sale and 27 lease.
- Source totals reported: 2,042 sale and 27 lease, with 2,550 rows across all public RCM project types.
- Detail coverage in artifact: 1,900 unique brokers, 416 URL-only document rows, 40,213 image URLs, 5,664 detailed contact rows, and 37 nonfatal `detailError` rows.
- Gated agreement, executive-summary, brochure, and deal-room labels were retained only in raw metadata unless exposed as public card links.

Ingest proof:

- Dry-run staged 1,836 rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.
- `cre_ingest.py` mapped source key `cbre-dealflow` into brokerage slug `cbre` with `dealflow:` external IDs.
- Active Deal Flow-prefixed rows after ingest: 1,857, with 1,830 sale and 27 lease. The 21-row difference from this full artifact is prior additive probe inventory, not a new load mismatch.
- Active Deal Flow-prefixed child rows after ingest: 5,597 contacts, 416 documents, and 40,176 images.
- Active Deal Flow-prefixed quality checks: 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad state codes, 0 impossible coordinates, 0 bad cap rates, and 0 orphan contacts/documents/images.
- Sample `search_cre_listings('industrial', null, 'TX', null, 'sale')` returned a live CBRE Deal Flow row, `Fort Worth Shallow Bay`.

Database note:

- psql reported the existing project-level collation version warning. That is outside the CRE loader and did not prevent validation queries or ingest.

## 2026-06-12 Newmark No-State Recovery Ingest

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
cp /tmp/newmark_no_state_full_probe.json out/newmark_full_2026-06-12_no_state_recovery.json
python3 cre_ingest.py --in out/newmark_full_2026-06-12_no_state_recovery.json --dry-run --keep-artifacts /tmp/newmark_full_2026-06-12_no_state_recovery_ingest_check
python3 cre_ingest.py --in out/newmark_full_2026-06-12_no_state_recovery.json --keep-artifacts /tmp/newmark_full_2026-06-12_no_state_recovery_live_ingest
```

Collector result:

- Artifact: `out/newmark_full_2026-06-12_no_state_recovery.json`.
- Collected rows: 4,371, including 1,121 sale and 3,250 lease.
- Source totals matched collected rows for both transactions.
- Artifact coverage: 4,303 image URLs, 0 document rows, 0 detailed contact rows, and 0 detail errors.

Ingest proof:

- Dry-run staged 4,371 rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.
- Latest Newmark batch in Supabase: 4,371 rows, 1,121 sale, 3,250 lease.
- Latest Newmark batch quality checks: 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad state codes, 0 impossible coordinates, 0 bad cap rates, 4,303 image child rows, and 0 orphan image rows.
- Active Newmark rows after ingest: 5,086, because earlier additive rows remain active pending a clean all-source reconciliation.

## 2026-06-12 Avison Young SharpLaunch Full Ingest

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=avison-young --transaction=both --max-items=0 --concurrency=4 --out=out/avison_full_2026-06-12_043342.json
python3 cre_ingest.py --in out/avison_full_2026-06-12_043342.json --dry-run --keep-artifacts /tmp/avison_full_2026-06-12_043342_ingest_check
python3 cre_ingest.py --in out/avison_full_2026-06-12_043342.json --keep-artifacts /tmp/avison_full_2026-06-12_043342_live_ingest
```

Collector result:

- Artifact: `out/avison_full_2026-06-12_043342.json`.
- Log: `out/avison_full_2026-06-12_043342.log`.
- Collected raw rows: 2,333, including 769 sale-bucket rows and 1,564 lease-bucket rows.
- Run-level brokers: 528.
- Artifact coverage: 2,318 image URLs, 4,376 detailed contact rows, 0 document rows, and 0 detail errors.

Ingest proof:

- Dry-run staged 2,200 unique rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.
- Active Avison Young rows after ingest: 2,200.
- Transaction split: 636 sale, 1,431 lease, and 133 `sale_or_lease`.
- Latest-batch quality checks: 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad state codes, 0 impossible coordinates, 0 bad cap rates, 4,125 contact child rows, 2,186 image child rows, and 0 orphan contact/image rows.

Remaining limit:

- The full SharpLaunch feed is now loaded, but optional detail-page enrichment is
  still needed for public PDFs, richer galleries, JSON-LD, and VCard/profile
  URLs.

## 2026-06-12 NAI Global Active Infabode Ingest

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=nai-global --transaction=both --max-items=24 --page-cap=6 --concurrency=2 --out=out/nai_active_filter_probe_2026-06-12.json
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --dry-run --keep-artifacts /tmp/nai_active_only_2026-06-12_ingest_check
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --keep-artifacts /tmp/nai_active_only_2026-06-12_live_ingest
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/nai_active_only_2026-06-12_mark_missing_check
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/nai_active_only_2026-06-12_mark_missing_live
```

Collector and policy result:

- Existing full enriched artifact `out/nai_full_unbounded_2026-06-12_044310.json`
  collected 13,597 public Infabode rows, but status review showed most rows were
  historical or ambiguous.
- Public `PostFilter` has no server-side `listingStatus` filter, so the safe
  policy is to detail-enrich through `publicPost(id)` and retain only rows whose
  public `listingStatus` contains `FOR_SALE_ON_MARKET`.
- Filtered active artifact:
  `out/nai_active_only_from_full_2026-06-12_044310.json`.
- Active artifact rows: 241 total, 183 sale and 58 lease.
- Statuses: 241 `FOR_SALE_ON_MARKET`; 0 `UNKNOWN`, `SOLD`, `UNDER_OFFER`, null,
  or detail-error rows retained.
- Artifact coverage: 0 missing URLs, 0 missing titles, 670 image URLs, 1
  document URL, 241 original/source website URLs, and 0 detailed contacts. The
  public API did not expose broker names, phones, profile URLs, or VCards in the
  sampled fields.

Ingest proof:

- Dry-run staged 241 rows and skipped 0 missing URLs.
- Live ingest completed, then source-scoped `--mark-missing` was applied only
  for `nai-global` after a dry-run confirmed the guard.
- Active Supabase rows after cleanup: 241, with 183 sale and 58 lease.
- The old 19 rendered-card probe rows with shared widget URLs were soft-deleted.
- Supabase validation found 0 missing URLs, 0 missing titles, 0 missing raw data,
  0 non-`FOR_SALE_ON_MARKET` statuses, 0 duplicate external IDs, 0 bad state
  codes, 0 impossible coordinates, and 0 orphan contacts/documents/images.
- Active child rows: 670 image URL rows, 1 document URL row, and 0 contact rows.
- `search_cre_listings('NAI', null, null, null, null)` returned live NAI Global
  rows with stable `https://infabode.com/services/listings/<id>` source URLs.

Remaining limit:

- The public Infabode feed also exposes older `UNKNOWN`, `SOLD`, `UNDER_OFFER`,
  null, and other non-active statuses back to 2021. Those rows are public but
  are not defensible active inventory. Save them only to audit/archive artifacts
  unless EQUIRE adds a separate historical listing surface.

## 2026-06-12 Cushman & Wakefield Full Run And Live Ingest

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=cushman-wakefield --transaction=both --max-items=0 --page-cap=400 --concurrency=6 --out=out/cushman_full_2026-06-12_022841.json
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --dry-run --keep-artifacts /tmp/cushman_full_2026-06-12_022841_ingest_check
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/cushman_full_2026-06-12_022841_mark_missing_check
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/cushman_full_2026-06-12_022841_mark_missing_live
```

Collector result:

- Artifact: `out/cushman_full_2026-06-12_022841.json`, 43.2 MB.
- Runtime: 4:41:00.
- Collected rows: 11,318, including 2,743 sale and 8,575 lease.
- Source totals matched collected rows for both transactions.
- Brokers: 1,696 unique run-level brokers.
- Artifact coverage: 18,343 document URLs, 24,278 image URLs, 21,110 detailed
  contacts, 21,110 profile URLs, 20,301 VCard URLs, 0 detail errors, 0 missing
  URLs, and 0 missing titles.

Ingest proof:

- Dry-run staged 11,318 rows and skipped 0 missing URLs.
- Source-scoped `--mark-missing` dry-run activated only for
  `cushman-wakefield`.
- Live ingest plus reconciliation completed.
- Active Cushman rows after ingest: 11,318, with 2,743 sale and 8,575 lease.
- Old shallow/probe rows soft-deleted: 24.
- Supabase validation found 0 missing URLs, 0 missing titles, 0 missing raw data,
  0 duplicate external IDs, 0 bad state codes, 0 impossible coordinates, 0
  malformed guarded prices, 0 malformed cap rates, and 0 orphan
  contacts/documents/images.
- Active child rows: 24,278 image URL rows, 18,343 document URL rows, 21,110
  contact rows, 21,110 profile URLs, and 20,301 VCard URLs.
- `search_cre_listings('Cushman', null, null, null, null)` returned live
  Cushman & Wakefield rows with stable property detail URLs.

Remaining limit:

- This is complete for the public Cushman search API and visible detail-page
  enrichment. Any future change should be a field audit, not a bulk coverage
  blocker.

## 2026-06-12 Transwestern Full Run And Live Ingest

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=transwestern --transaction=both --max-items=0 --concurrency=4 --out=out/transwestern_full_2026-06-12_121302.json
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --dry-run --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_ingest_check
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_mark_missing_check
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_mark_missing_live_retry
```

Collector and cleanup result:

- Raw artifact: `out/transwestern_full_2026-06-12_121302.json`, 8.1 MB.
- Cleaned ingest artifact:
  `out/transwestern_full_2026-06-12_121302_cleaned.json`.
- Collected rows: 2,151 raw rows, with 519 sale-bucket rows and 1,632
  lease-bucket rows.
- The 130 `Sale or Lease` rows intentionally appeared in both sale and lease
  passes and merged to `sale_or_lease` during staging.
- Detail coverage before ingest: 3,184 document URLs, 5,093 image URLs, 3,963
  contacts/profile URLs/VCard URLs, 0 detail errors, 0 missing URLs, and 0
  missing titles.
- The cleaned artifact removed 2,151 footer/TREC/copyright descriptions because
  the detail fallback was site footer text, not property narrative text.
- After cleanup: 0 bad descriptions, 0 bad document URLs, and 0 bad image URLs.

Ingest proof:

- Dry-run staged 2,021 unique rows and skipped 0 missing URLs.
- Initial live ingest failed because the live database had not yet seeded the
  existing `sql/001_cre_brokerages.sql` Transwestern row. The missing
  `credeals.cre_brokerages` slug was inserted, then the same cleaned ingest was
  retried.
- Source-scoped `--mark-missing` dry-run activated only for `transwestern`.
- Live ingest plus reconciliation completed.
- Active Transwestern rows after ingest: 2,021, with 389 sale, 1,502 lease, and
  130 sale_or_lease.
- Supabase validation found 0 missing URLs, 0 missing titles, 0 missing raw data,
  0 bad descriptions, 0 duplicate external IDs, 0 bad state codes, 0 impossible
  coordinates, 0 malformed guarded prices, 0 malformed cap rates, 0 bad document
  URLs, 0 bad image URLs, and 0 orphan contacts/documents/images.
- Active child rows: 4,838 image URL rows, 3,054 document URL rows, 3,746
  contact rows, 3,746 profile URLs, and 3,746 VCard URLs.
- `credeals.v_cre_listings_full` returned live Transwestern sample rows, and
  `search_cre_listings('National Avenue', null, null, null, null)` returned the
  live `1025 W. National Avenue` Transwestern listing.

Remaining limit:

- Availability parsing and price/rate promotion should be hardened before daily
  scheduling. The full live load is still defensible because raw availability is
  retained and guarded malformed prices/rates validate cleanly.
