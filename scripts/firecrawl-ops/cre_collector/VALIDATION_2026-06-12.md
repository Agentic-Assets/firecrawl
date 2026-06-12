# CRE Collector Supabase Validation - 2026-06-12

Validation time: 2026-06-12 local time.

## Verdict

The latest full collection was uploaded correctly for the rows it safely staged, but the system does not yet contain every public listing from every target brokerage.

The latest full artifact staged 33,488 unique rows and the live Supabase rows touched by that artifact also total 33,488. The active listing view currently has 34,218 rows because 730 older rows remain active from prior additive runs. This was intentional because `--mark-missing` was not used after Lee & Associates failed.

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

- Post-validation code update: Cushman & Wakefield no longer uses the shallow rendered Coveo card path. On 2026-06-12 local probes verified the public `/api/properties/search` path with 2,743 sale and 8,575 lease live source totals. A targeted `CUSHMAN_QUERY='1800 Central'` probe captured the expected 2 PDFs, 15 photos, building size, lot size, year built, and Gib Kerr contact/profile/VCard data. These rows are not yet reflected in the validated Supabase counts above.
- Post-validation live ingest: CBRE Deal Flow was upgraded from the old first-grid path to the public RCM ListingEngine endpoint and ingested additively from `out/cbre_dealflow_full_2026-06-12_041740.json`. The artifact staged 1,836 rows, 1,809 public sale cards and all 27 public lease cards, with 0 skipped missing URLs. RCM reported 2,042 sale rows, but public card pagination exposed 1,809 sale cards before returning 0 additional cards. Live Supabase now has 1,857 active Deal Flow-prefixed rows under brokerage slug `cbre`, including prior additive probe rows retained because `--mark-missing` was not used.
- Post-validation live ingest: Newmark no-state recovery was ingested additively from `out/newmark_full_2026-06-12_no_state_recovery.json`. The artifact staged 4,371 rows, 1,121 sale and 3,250 lease, with 0 skipped missing URLs. Latest-batch validation found 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad states, 0 bad coordinates, 0 bad cap rates, 4,303 image child rows, and 0 orphan images.
- Post-validation live ingest: Avison Young SharpLaunch full feed was ingested additively from `out/avison_full_2026-06-12_043342.json`. The artifact collected 2,333 raw rows and staged 2,200 unique rows after dual sale/lease merge, with 0 skipped missing URLs. Latest-batch validation found 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad states, 0 bad coordinates, 0 bad cap rates, 4,125 contact child rows, 2,186 image child rows, and 0 orphan contact/image rows.
- Lee & Associates is not uploaded. A fresh Lee-only run on 2026-06-12 passed the prior failure zone, then failed pages 286 through 297 after retries and aborted with `Error: no listings collected from any source`. It wrote only `out/lee_latest_2026-06-12_004010.log`, not a usable JSON artifact.
- Colliers is not uploaded. Current collector has no supported public GET path for the POST-only workflow.
- Transwestern is not uploaded from a full run yet. Current collector has a targeted public GET feed probe and dry-run proof, but it still needs full collection, live ingest, and Supabase validation.
- 730 older active rows remain from earlier additive runs: Newmark 715, Marcus & Millichap 6, CBRE 5, Savills 2, SVN 2. Do not treat active row count as a pure latest-run count until a clean reconciliation run marks missing rows.
- Some supported adapters are intentionally shallow: Avison Young, Marcus & Millichap, NAI Global, and Savills have first-page, first-batch, or sale-only limitations documented in `CLAUDE.md`. Cushman was removed from this list after the 2026-06-12 API upgrade, pending full re-run and ingest.

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
4. Treat Colliers as integration backlog until a permitted API path exists. Run Transwestern full collection and validation from the public GET feed before marking it complete.

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
