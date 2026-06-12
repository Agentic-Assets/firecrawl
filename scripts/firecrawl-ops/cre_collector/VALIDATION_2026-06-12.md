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
- Lee & Associates is not uploaded. A fresh Lee-only run on 2026-06-12 passed the prior failure zone, then failed pages 286 through 297 after retries and aborted with `Error: no listings collected from any source`. It wrote only `out/lee_latest_2026-06-12_004010.log`, not a usable JSON artifact.
- Colliers is not uploaded. Current collector has no supported public GET path for the POST-only workflow.
- Transwestern is not uploaded. Current collector has no supported public GET path for the POST-only workflow.
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
4. Treat Colliers and Transwestern as integration backlog, not current coverage, until a permitted API path exists.
