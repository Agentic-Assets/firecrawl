# CRE Collector Start Here

Last updated: 2026-06-12 local time, evidence from run finished at `2026-06-12T04:31:24.562Z`, validation on 2026-06-12, Cushman post-validation probes, CBRE Deal Flow plus Colliers SalesTracker full ingests, and NAI active-status-filtered ingest on 2026-06-12.

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
- `--mark-missing` was not used because Lee & Associates failed.
- Fresh validation confirmed 33,488 latest artifact rows touched in Supabase and 34,218 active rows total because 730 older additive rows remain active.

## Latest Source Matrix

| Source | Raw count | Status |
|---|---:|---|
| CBRE | 20,684 | Active |
| CBRE Deal Flow | 1,836 in post-validation full run, 1,809 sale + 27 lease | Active via public RCM ListingEngine endpoint and live-ingested additively |
| JLL | 4,678 | Active |
| JLL Investor | 8 in latest hardened probe; source total about 1,087 to 1,088 | Partial, first rendered search page detail-enriched |
| Cushman & Wakefield | 24 in latest ingested artifact; code now verifies 2,743 sale + 8,575 lease live source totals | Active via public API and detail enrichment; pending full re-run and ingest |
| Newmark | 4,371 in post-validation full probe | Active via Algolia, no-state recovery added |
| Marcus & Millichap | 12 in probe, 3,126 reported public sale total | Partial, public contentsearch sale API and detail enrichment; lease unsupported |
| Avison Young | 2,200 staged unique rows in post-validation full run | Public SharpLaunch feed live-ingested additively; still needs optional detail-page enrichment |
| Savills | 100 | Active, sale only; US lease empty after fallback filtering |
| SVN | 5,521 in latest full artifact | Mapping complete from prior full artifact; fresh live refresh partial due Buildout 403 HTML |
| NAI Global | 241 active rows, 183 sale + 58 lease live-ingested with mark-missing cleanup | Complete public active feed via Infabode GraphQL and `publicPost`, filtered to `FOR_SALE_ON_MARKET`; historical/unknown rows excluded |
| Lee & Associates | 0 | Blocked under sustained Buildout paging; latest retry failed pages 286-297 |
| Colliers | 1,300 SalesTracker cards collected, 1,172 unique rows live-ingested | Partial investment-sale coverage via public RCM GET endpoints; main Colliers Coveo sale/lease search remains blocked |
| Transwestern | 8 in probe, source feed totals 519 sale-bucket rows and 1,636 lease-bucket rows before dedupe | Public GET feed implemented and dry-run proven; pending full run and live ingest |

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
```

## Safe Daily Command

Use this while Lee remains blocked:

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
- Do not treat Supabase as current for Cushman until a fresh full collection and ingest runs. The code was upgraded after the latest ingest; local probes verified 2,743 sale, 8,575 lease, and the 1800 Central detail record with 2 PDFs, 15 photos, and contact links.
- CBRE Deal Flow has been ingested additively from the public RCM endpoint. Do not use its reported 2,042 sale total as collected count; the public card pagination exposed 1,809 sale cards in the full run.
- Do not store source PDF or image binaries in Supabase. Store URLs only.
- Do not claim complete Colliers coverage. Only SalesTracker investment-sale coverage has a public GET path; 1,172 unique SalesTracker sale rows are live-ingested, while the main Colliers Coveo sale/lease search remains blocked.
- Do not ingest NAI Global's unbounded Infabode feed as active inventory. Use only rows whose public `publicPost.listingStatus` contains `FOR_SALE_ON_MARKET`. The 2026-06-12 active artifact `out/nai_active_only_from_full_2026-06-12_044310.json` was live-ingested with source-scoped `--mark-missing`; 19 old rendered-card probe rows were soft-deleted.
- Do not claim Transwestern complete until the implemented public GET feed has a clean full run, live ingest, and Supabase validation.
- Do not claim Lee coverage until a sustained full Lee run writes a clean artifact and is ingested.
- Do not treat legacy `cre_scrapers` active flags as production collector status.
- Do not stage `node_modules/`, `out/`, `__pycache__/`, or generated SQL artifacts.
