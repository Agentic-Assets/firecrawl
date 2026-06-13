# CRE Collector Start Here

Last updated: 2026-06-12 local time, evidence from run finished at `2026-06-12T04:31:24.562Z`, validation on 2026-06-12, CBRE Deal Flow plus Colliers SalesTracker full ingests, NAI active-status-filtered ingest, Cushman full live ingest, Transwestern full live ingest, Marcus & Millichap full public sale ingest, and Lee & Associates full Buildout ingest on 2026-06-12. JLL Investor full sitemap detail ingest finished 2026-06-12 22:47 UTC (934 U.S. sale rows live); 50 stale early-probe rows soft-deleted 2026-06-12 ~23:25 UTC after user approval. Avison Young full detail-enriched ingest finished 2026-06-13 00:35 UTC and was live-ingested additively with 2,201 active rows.

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
- After later source-specific ingests through JLL full detail enrichment,
  JLL Investor full sitemap detail ingest, and narrow stale-row cleanups,
  live Supabase active rows total 71,600 as of 2026-06-12T23:26 UTC.

## Latest Source Matrix

| Source | Raw count | Status |
|---|---:|---|
| CBRE | 19,028 active rows, 4,222 sale + 13,145 lease + 1,661 sale_or_lease | Active via internal JSON API through local Firecrawl stealth |
| CBRE Deal Flow | 1,836 active rows, 1,809 sale + 27 lease | Active via public RCM ListingEngine endpoint; 21 stale URL-hash duplicate rows soft-deleted |
| JLL | 10,741 active rows, 1,247 sale + 8,733 lease + 761 sale_or_lease | Complete main public property feed with detail enrichment, live-ingested; 4,406 stale same-URL rows soft-deleted |
| JLL Investor | 1,857 sitemap detail URLs scanned; 934 U.S. sale rows retained and live (latest batch) | Complete; full sitemap detail run live-ingested 2026-06-12 22:47 UTC; source-scoped cleanup removed 50 stale early-probe rows |
| Cushman & Wakefield | 11,318 active rows, 2,743 sale + 8,575 lease | Complete public API feed with detail enrichment, live-ingested with source-scoped mark-missing cleanup |
| Newmark | 4,371 active rows, 1,121 sale + 3,250 lease | Complete public Algolia feed with no-state DC recovery, public People contacts/profile URLs, raw hit preservation, and source-scoped cleanup |
| Marcus & Millichap | 3,124 active sale rows | Complete public sale feed via public map ActivityIds, `mappropertydetail` tiles, and detail HTML; live-ingested with source-scoped mark-missing cleanup; lease unsupported |
| Avison Young | 2,201 active rows, 636 sale + 1,432 lease + 133 sale_or_lease | Complete public SharpLaunch feed with detail-page enrichment, live-ingested additively; 2,571 document URL rows, 31,570 image URL rows, 4,128 contacts, no photo leaks, VCards absent |
| Savills | 104 active rows, 101 sale + 3 lease | Partial; 3 U.S. retail lease rows are now live with PDF/image/contact URLs, while current sale rows remain global/residential and not CRE-defensible |
| SVN | 5,287 active rows, 2,660 sale + 2,192 lease + 435 sale_or_lease | Complete public Buildout feed, assembled from durable page cache and live-ingested with source-scoped mark-missing cleanup |
| NAI Global | 241 active rows, 183 sale + 58 lease live-ingested with mark-missing cleanup | Complete public active feed via Infabode GraphQL and `publicPost`, filtered to `FOR_SALE_ON_MARKET`; historical/unknown rows excluded |
| Lee & Associates | 9,223 active rows, 2,611 sale + 5,691 lease + 921 sale_or_lease | Complete public Buildout feed, assembled from durable page cache and live-ingested with source-scoped mark-missing cleanup |
| Colliers (SalesTracker) | 1,300 SalesTracker cards collected, 1,172 unique rows live-ingested | Investment-sale subset via public RCM GET endpoints; retained alongside the new main-site source |
| Colliers main (`colliers-main`) | 15,896 sitemap detail URLs discovered; bounded 2,000-URL batch live (943 rows: 346 sale, 518 lease, 79 sale_or_lease) | Main `www.colliers.com` unblocked via public XML sitemap (`/sitemap` -> `en/sitemap?type=properties`) plus detail-render JSON-LD parse, folded into `colliers` with `main:` prefix. Full ~15,896 run in progress 2026-06-13. See `HANDOFF_COLLIERS_MAIN_2026-06-13.md` |
| Transwestern | 2,021 active rows, 389 sale + 1,502 lease + 130 sale_or_lease | Complete public GET feed, detail-enriched and live-ingested with source-scoped mark-missing cleanup |

## Start A New Session

Read these in order:

1. `AGENTS.md`
2. `scripts/firecrawl-ops/CLAUDE.md`
3. `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
4. This file
5. `BROKERAGE_STATUS_2026-06-12.md` (live per-broker coverage and counts)
6. `docs/firecrawl-ops/references/cre-intelligence-system-design.md` (canonical architecture + go-forward monitoring plan, section 14)
7. `docs/firecrawl-ops/references/cre-equire-consumer-api.md` (how EQUIRE reads the data: views, SQL, env, quick start)
8. `docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md` (reusable per-source completion process)
9. `HANDOFF_COLLIERS_MAIN_2026-06-13.md` (active handoff: colliers-main full run in progress)

Historical buildout/validation detail (handoff log, lessons, validation
snapshots, egress and security audits) lives in `archive/`; see
`archive/README.md` for the index and the durable nuggets each file still holds.

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
`archive/CONTRACT_SYNC_2026-06-12.md` plus the later UI-side hardening SQL notes.

Document and image tables store source URLs only. Do not download public PDFs or
images into Supabase storage for the bulk collector.

## Known Limits To Respect

- Do not use `--mark-missing` after a run with Lee or other source errors.
- Cushman & Wakefield is now current in Supabase from `out/cushman_full_2026-06-12_022841.json`: 11,318 active rows, 18,343 document URL rows, 24,278 image URL rows, 21,110 contact rows, 21,110 profile URLs, and 20,301 VCard URLs. Source-scoped `--mark-missing` soft-deleted 24 old probe rows.
- CBRE Deal Flow has been ingested from the public RCM endpoint. Do not use its reported 2,042 sale total as collected count; the public card pagination exposed 1,809 sale cards in the full run. A narrow cleanup soft-deleted 21 stale `dealflow:url:<sha1>` rows that duplicated newer enriched Deal Flow IDs.
- Do not store source PDF or image binaries in Supabase. Store URLs only.
- Colliers now has two folded sources under the `colliers` brokerage. SalesTracker (`colliers`, 1,172 investment-sale rows) via public RCM GET. Main site (`colliers-main`) via the public XML sitemap (`/sitemap` -> `en/sitemap?type=properties`, 15,896 detail URLs) fetched through local Firecrawl plus detail-render JSON-LD parse; ids prefixed `main:`. The Coveo POST search is still not used and not needed. A bounded 2,000-URL batch is live (943 rows); the full run is in progress as of 2026-06-13 (`HANDOFF_COLLIERS_MAIN_2026-06-13.md`). Do not claim complete main-site coverage until the full run is ingested and validated.
- Do not ingest NAI Global's unbounded Infabode feed as active inventory. Use only rows whose public `publicPost.listingStatus` contains `FOR_SALE_ON_MARKET`. The 2026-06-12 active artifact `out/nai_active_only_from_full_2026-06-12_044310.json` was live-ingested with source-scoped `--mark-missing`; 19 old rendered-card probe rows were soft-deleted.
- Transwestern is now current in Supabase from `out/transwestern_full_2026-06-12_121302_cleaned.json`: 2,021 active rows, 3,054 document URL rows, 4,838 image URL rows, 3,746 contact/profile/VCard URL rows, and 0 bad descriptions or bad asset URLs. The live DB needed the existing `sql/001_cre_brokerages.sql` Transwestern seed inserted before ingest.
- Marcus & Millichap is now current in Supabase from `out/marcus_full_2026-06-12_130035.json`: 3,124 active public sale rows, 16,771 image URL rows, 7,915 contact/profile URL rows, 0 document rows, and 0 final detail errors. Gated deal-room URLs stay in raw metadata only. Public lease remains unsupported.
- Lee & Associates is now current in Supabase from `out/lee_full_cache_2026-06-12_assembled.json`: 9,223 active rows, 9,062 image URL rows, 7,681 document URL rows, 9,223 contact rows, and 0 bad URLs, duplicate IDs, bad states, bad coordinates, or child orphans. The durable Buildout cache remains under gitignored `out/cache/buildout/lee-associates/`.
- Newmark is now current in Supabase from `out/newmark_full_refined_2026-06-12.json`: 4,371 active rows, 4,303 image URL rows, 3,961 contact/profile URL rows, 0 document rows, 0 missing states, and 715 old additive rows soft-deleted. Listing documents, full galleries, second/third broker joins, and VCards remain unproven.
- SVN is now current in Supabase from `out/svn_full_cache_2026-06-12_assembled.json`: 5,287 active rows, 5,235 image URL rows, 3,899 document URL rows, 5,287 contact rows, 0 duplicate external IDs, 0 bad URLs, 0 missing titles, 0 missing raw data, and 34 old rows soft-deleted. One active SVN row is missing state.
- JLL main property feed is now current in Supabase from `out/jll_full_detail_enriched_2026-06-12.json`: 11,230 collected sale/lease rows, 10,604 staged unique rows, 0 detail errors, 0 skipped missing URLs, 9,747 artifact document URLs, 28,254 artifact image URLs, and 23,801 artifact contacts/profile URLs. Live JLL main now has 10,741 active rows after 4,406 old same-URL rows were soft-deleted. Remaining duplicate source URL groups are 135 latest-batch sale/lease same-page variants.
- JLL Investor Center is now current in Supabase from `out/jll_investor_full_sitemap_detail_2026-06-12.json`: 934 active rows (all sale; lease not applicable for this path), 2,572 contact rows, 345 document URL rows, and 5,658 image URL rows. All jll-investor rows lack coordinates because the Investor detail path exposes none (known limitation, not a regression). Speed controls: `JLL_INVESTOR_DETAIL_WAIT_MS=1000`, `JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS=8000`, `JLL_INVESTOR_DETAIL_CONCURRENCY=4` (commit d0c9f5d63). The 50 stale early-probe rows were soft-deleted after user approval at ~23:25 UTC 2026-06-12.
- Avison Young is now current in Supabase from
  `out/avison_full_detail_2026-06-12.json`: 2,201 active rows, 2,571 document
  URL rows, 31,570 image URL rows, 4,128 contacts, 0 detail errors in the
  artifact field, and 0 non-property photo leaks after the Avison-specific photo
  filter fix. The full detail run used `AVISON_YOUNG_DETAIL_LIMIT=2200` and was
  live-ingested additively without `--mark-missing`. VCards remain absent from
  the public path, and broker profile URLs are sparse.
- Savills commercial lease path is live-ingested additively from
  `out/savills_lease_public_2026-06-12_live_candidate.json`: 3 U.S. retail
  lease rows live (updated from the original 2-row ingest artifact), 4 PDF URL rows,
  24 image URL rows, and 0 skipped missing URLs. Savills sale remains not CRE-defensible.
- `cre_ingest.py` now drops non-HTTP contact profile/avatar/VCard URLs and
  non-HTTP document URLs. Reingesting the complete Lee and SVN artifacts
  refreshed child rows and reduced active bad contact avatar URLs from 37 to 0.
- `cre_ingest.py --mark-missing` now refuses incomplete folded source coverage.
  For parent slugs with sub-sources, such as `cbre` plus `cbre-dealflow` or
  `jll` plus `jll-investor`, all known source keys must be present in the same
  ingest batch before parent-level soft deletes can activate.
- Do not treat legacy `cre_scrapers` active flags as production collector status.
- Do not stage `node_modules/`, `out/`, `__pycache__/`, or generated SQL artifacts.
