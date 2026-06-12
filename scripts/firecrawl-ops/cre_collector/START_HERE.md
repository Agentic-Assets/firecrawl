# CRE Collector Start Here

Last updated: 2026-06-12 local time, evidence from run finished at `2026-06-12T04:31:24.562Z`, validation on 2026-06-12, CBRE Deal Flow plus Colliers SalesTracker full ingests, NAI active-status-filtered ingest, Cushman full live ingest, Transwestern full live ingest, Marcus & Millichap full public sale ingest, and Lee & Associates full Buildout ingest on 2026-06-12.

This directory is the production daily path for public commercial real estate listing inventory feeding EQUIRE. Use it for sale and lease listings. The older `../cre_scrapers/` Python package is legacy support for source probes and detail-page enrichment.

## Current State

Latest full artifact:

```bash
out/full_latest_2026-06-11_230423.json
```

Latest full command:

```bash
npx tsx collect.ts --source=all --transaction=both --max-items=0 --page-cap=400 --concurrency=3 --out=out/full_latest_2026-06-11_230423.json
```

Result:

- 35,510 raw listing records.
- 33,488 unique staged upsert rows.
- 3,878 unique brokers.
- 41.6 MB artifact.
- 27:01.56 wall time.
- Live additive ingest completed through `psql`.
- `--mark-missing` was not used on that all-source run because Lee & Associates failed at the time; Lee was later completed through a source-specific cache assembly run.
- Fresh validation confirmed 33,488 latest artifact rows touched in Supabase and 34,218 active rows total because 730 older additive rows remained active before later source-specific reconciliations.
- After later source-specific ingests through SVN reconciliation, live Supabase
  active rows total 64,537.

## Latest Source Matrix

| Source | Raw count | Status |
|---|---:|---|
| CBRE | 20,684 | Active |
| CBRE Deal Flow | 1,836 in post-validation full run, 1,809 sale + 27 lease | Active via public RCM ListingEngine endpoint and live-ingested additively |
| JLL | 4,678 | Active |
| JLL Investor | 8 in latest hardened probe; source total about 1,087 to 1,088 | Partial, first rendered search page detail-enriched |
| Cushman & Wakefield | 11,318 active rows, 2,743 sale + 8,575 lease | Complete public API feed with detail enrichment, live-ingested with source-scoped mark-missing cleanup |
| Newmark | 4,371 active rows, 1,121 sale + 3,250 lease | Complete public Algolia feed with no-state DC recovery, public People contacts/profile URLs, raw hit preservation, and source-scoped cleanup |
| Marcus & Millichap | 3,124 active sale rows | Complete public sale feed via public map ActivityIds, `mappropertydetail` tiles, and detail HTML; live-ingested with source-scoped mark-missing cleanup; lease unsupported |
| Avison Young | 2,200 staged unique rows in post-validation full run | Public SharpLaunch feed live-ingested additively; bounded detail enrichment is verified for selected rows but full-feed detail enrichment has not been live-run |
| Savills | 100 legacy sale rows + 2 defensible commercial lease rows in latest probe | Partial; lease now has 2 U.S. retail rows with PDF/image/contact URLs, while current sale rows remain global/residential and not CRE-defensible |
| SVN | 5,287 active rows, 2,660 sale + 2,192 lease + 435 sale_or_lease | Complete public Buildout feed, assembled from durable page cache and live-ingested with source-scoped mark-missing cleanup |
| NAI Global | 241 active rows, 183 sale + 58 lease live-ingested with mark-missing cleanup | Complete public active feed via Infabode GraphQL and `publicPost`, filtered to `FOR_SALE_ON_MARKET`; historical/unknown rows excluded |
| Lee & Associates | 9,223 active rows, 2,611 sale + 5,691 lease + 921 sale_or_lease | Complete public Buildout feed, assembled from durable page cache and live-ingested with source-scoped mark-missing cleanup |
| Colliers | 1,300 SalesTracker cards collected, 1,172 unique rows live-ingested | Partial investment-sale coverage via public RCM GET endpoints; main Colliers Coveo sale/lease search remains blocked |
| Transwestern | 2,021 active rows, 389 sale + 1,502 lease + 130 sale_or_lease | Complete public GET feed, detail-enriched and live-ingested with source-scoped mark-missing cleanup |

## Start A New Session

Read these in order:

1. `AGENTS.md`
2. `scripts/firecrawl-ops/CLAUDE.md`
3. `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
4. This file
5. `HANDOFF_LOG_2026-06-11.md`
6. `LESSONS_2026-06-11.md`
7. `VALIDATION_2026-06-12.md`
8. `BROKERAGE_STATUS_2026-06-12.md`
9. `SUPABASE_SECURITY_NOTE_2026-06-12.md`
10. `CONTRACT_SYNC_2026-06-12.md`
11. `SUPABASE_EGRESS_AUDIT_2026-06-12.md`
12. `docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`

Then run:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
python3 -m py_compile cre_ingest.py
npm run validate:supabase -- --out /tmp/cre_validate_latest.md
```

## Safe Daily Command

Use this while any all-source errors or partial source decisions remain:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
bash cre_daily_update.sh --no-mark-missing
```

Use default `bash cre_daily_update.sh` only after a clean all-source run has no Lee/source errors and the per-broker mark-missing guards are acceptable for that day.

## Supabase Access Model

Target project: `fhqycqubkkrdgzswccwd`, schema `credeals`.

The ingestor reads `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from the EQUIRE `.env.local` file and shells out to `psql`. It prints only the env file path, never the credential value.

The collector-owned `cre_*` base tables and `v_cre_*` views are service-role only. `anon` and `authenticated` do not have table or view `SELECT`. RLS is enabled with no public row policies by design. The display views use `security_invoker=true`, and `search_cre_listings(...)` plus `update_cre_listing_timestamp()` are executable by `service_role`, not by public browser roles.

If the UI-side live-board plan docs disagree with this posture, prefer
`CONTRACT_SYNC_2026-06-12.md` plus the later UI-side hardening SQL notes.

Document and image tables store source URLs only. Do not download public PDFs or
images into Supabase storage for the bulk collector.

## Known Limits To Respect

- Do not use `--mark-missing` after a run with Lee or other source errors.
- Cushman & Wakefield is now current in Supabase from `out/cushman_full_2026-06-12_022841.json`: 11,318 active rows, 18,343 document URL rows, 24,278 image URL rows, 21,110 contact rows, 21,110 profile URLs, and 20,301 VCard URLs. Source-scoped `--mark-missing` soft-deleted 24 old probe rows.
- CBRE Deal Flow has been ingested additively from the public RCM endpoint. Do not use its reported 2,042 sale total as collected count; the public card pagination exposed 1,809 sale cards in the full run.
- Do not store source PDF or image binaries in Supabase. Store URLs only.
- Do not claim complete Colliers coverage. Only SalesTracker investment-sale coverage has a public GET path; 1,172 unique SalesTracker sale rows are live-ingested, while the main Colliers Coveo sale/lease search remains blocked.
- Do not ingest NAI Global's unbounded Infabode feed as active inventory. Use only rows whose public `publicPost.listingStatus` contains `FOR_SALE_ON_MARKET`. The 2026-06-12 active artifact `out/nai_active_only_from_full_2026-06-12_044310.json` was live-ingested with source-scoped `--mark-missing`; 19 old rendered-card probe rows were soft-deleted.
- Transwestern is now current in Supabase from `out/transwestern_full_2026-06-12_121302_cleaned.json`: 2,021 active rows, 3,054 document URL rows, 4,838 image URL rows, 3,746 contact/profile/VCard URL rows, and 0 bad descriptions or bad asset URLs. The live DB needed the existing `sql/001_cre_brokerages.sql` Transwestern seed inserted before ingest.
- Marcus & Millichap is now current in Supabase from `out/marcus_full_2026-06-12_130035.json`: 3,124 active public sale rows, 16,771 image URL rows, 7,915 contact/profile URL rows, 0 document rows, and 0 final detail errors. Gated deal-room URLs stay in raw metadata only. Public lease remains unsupported.
- Lee & Associates is now current in Supabase from `out/lee_full_cache_2026-06-12_assembled.json`: 9,223 active rows, 9,062 image URL rows, 7,681 document URL rows, 9,223 contact rows, and 0 bad URLs, duplicate IDs, bad states, bad coordinates, or child orphans. The durable Buildout cache remains under gitignored `out/cache/buildout/lee-associates/`.
- Newmark is now current in Supabase from `out/newmark_full_refined_2026-06-12.json`: 4,371 active rows, 4,303 image URL rows, 3,961 contact/profile URL rows, 0 document rows, 0 missing states, and 715 old additive rows soft-deleted. Listing documents, full galleries, second/third broker joins, and VCards remain unproven.
- SVN is now current in Supabase from `out/svn_full_cache_2026-06-12_assembled.json`: 5,287 active rows, 5,235 image URL rows, 3,899 document URL rows, 5,287 contact rows, 0 duplicate external IDs, 0 bad URLs, 0 missing titles, 0 missing raw data, and 34 old rows soft-deleted. One active SVN row is missing state.
- Avison Young bounded detail enrichment is verified on a 4-row probe:
  6 public PDF URLs, 36 public image URLs, 4 JSON-LD payloads, 1 broker profile
  URL, 0 VCards, and 0 detail errors. Full-feed runs stay SharpLaunch-only by
  default unless `AVISON_YOUNG_DETAIL_LIMIT` is set.
- `cre_ingest.py` now drops non-HTTP contact profile/avatar/VCard URLs and
  non-HTTP document URLs. Reingesting the complete Lee and SVN artifacts
  refreshed child rows and reduced active bad contact avatar URLs from 37 to 0.
- `cre_ingest.py --mark-missing` now refuses incomplete folded source coverage.
  For parent slugs with sub-sources, such as `cbre` plus `cbre-dealflow` or
  `jll` plus `jll-investor`, all known source keys must be present in the same
  ingest batch before parent-level soft deletes can activate.
- Do not treat legacy `cre_scrapers` active flags as production collector status.
- Do not stage `node_modules/`, `out/`, `__pycache__/`, or generated SQL artifacts.
