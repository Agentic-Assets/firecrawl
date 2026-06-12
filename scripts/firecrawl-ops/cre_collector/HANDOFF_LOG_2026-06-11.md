# CRE Collector Handoff Log - 2026-06-11

## Bigger Picture

The workstream is building a reusable commercial real estate listing supply line for EQUIRE. The target is not a one-off scrape. The target is a daily refresh system that collects public for-sale and for-lease inventory from major broker platforms, normalizes it into the Supabase `credeals` schema, and gives EQUIRE agents a durable listing database for sourcing, mandate-fit screening, market summaries, broker contact memory, and conversion from public candidate listing to internal deal work.

The production path now lives in `scripts/firecrawl-ops/cre_collector/`. It is intended to supersede the older one-broker-at-a-time Python scraper package for bulk collection, while the older `cre_scrapers/` package remains useful for source-specific experiments and detail-page enrichment.

## What Was Built

- `collect.ts` is the main TypeScript collector. It runs against the local self-hosted Firecrawl API at `http://localhost:3002`.
- `cre_ingest.py` converts collector JSON into SQL staged through `psql`, then upserts into Supabase project `fhqycqubkkrdgzswccwd`, schema `credeals`.
- `cre_daily_update.sh` wraps the daily flow: healthcheck, full collect, ingest, optional `--mark-missing`, and artifact pruning.
- `com.agenticassets.cre-daily.plist.example` gives a launchd template for a 06:30 local daily run.
- `CODEX_GOAL_2026-06-11.md` is the pasteable Codex goal for finishing the system.
- `scripts/firecrawl-ops/CLAUDE.md`, `cre_collector/CLAUDE.md`, root `CLAUDE.md`, SQL docs, and CRE reference docs were updated to point future agents at the new production path.

## Source Coverage State

Working or partially working collector sources:

- `cbre`: internal JSON API through local Firecrawl stealth, sale and lease.
- `cbre-dealflow`: public first-page grid, sale-oriented.
- `jll`: public search pages, sale and lease.
- `jll-investor`: investor center grid, sale-oriented.
- `cushman-wakefield`: rendered search page, currently limited by first-page or Coveo pagination behavior.
- `newmark`: public Algolia index, sale and lease, with state and property-type sub-splitting to avoid the 1,000-hit cap.
- `marcus-millichap`: rendered grid under stealth with retries, sale-oriented.
- `avison-young`: SPA sidebar, currently first rendered batch only.
- `savills`: server-rendered pages, sale listings; US lease appears empty and foreign fallback cards are filtered.
- `svn`: Buildout inventory JSON API, client-side sale/lease partitioning.
- `lee-associates`: Buildout inventory JSON API, same engine as SVN, but rate-limit behavior needs more verification.
- `nai-global`: Infabode widget cards, first rendered batch only, synthesized card hash ids because cards lack stable links.

Unsupported in the current collector:

- `colliers`: usable path appears to require POST-only API access.
- `transwestern`: usable path appears to require POST-only API access.

## Verification Already Run In This Continuation

- Read the exported transcript at `scripts/firecrawl-ops/cre_collector/2026-06-11-213703-this-session-is-being-continued-from-a-previous-c.txt`.
- Inspected current collector files, docs, SQL docs, and existing output artifacts.
- Checked current Codex goal guidance from OpenAI docs before writing the goal.
- Checked Supabase changelog context for current Data API exposure behavior.
- Ran `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh`: local stack passed. API, queue, and scrape smoke were healthy.
- Ran a small live probe:
  - Command shape: `npx tsx collect.ts --source=savills,nai-global,newmark --transaction=both --max-items=3 --page-cap=5 --concurrency=2 --out=/tmp/cre_goal_probe.json`
  - Result: 15 listings, 6 source entries, no errors.
  - Savills lease returned 0 US listings, which matches the current empty-US-lease behavior after fallback filtering.
- Ran ingest dry run:
  - `python3 cre_ingest.py --in /tmp/cre_goal_probe.json --dry-run --keep-artifacts /tmp/cre_goal_probe_ingest`
  - Result: 15 staged listings, 0 skipped for missing URL.
- Ran `python3 -m py_compile scripts/firecrawl-ops/cre_collector/cre_ingest.py`: passed.
- Tried an ad hoc TypeScript validation with `typescript`: failed because the collector package lacks pinned TypeScript and Node type definitions. This is a tooling gap, not proof of a collector runtime failure.

## Existing Full Run Artifacts Observed

Existing artifacts under `scripts/firecrawl-ops/cre_collector/out/` show prior full or grouped runs:

- `full_stealth_2026-06-11.json`: 20,718 listings from CBRE, CBRE Deal Flow, and Marcus & Millichap.
- `full_jll_2026-06-11.json`: 4,727 listings from JLL and JLL Investor.
- `full_api_2026-06-11.json`: 9,981 listings from SVN, Newmark, Savills, and NAI Global, with Lee & Associates Buildout page failures recorded.
- `full_cw_ay_2026-06-11.json`: 46 listings from Cushman and Avison Young, reflecting the known limited rendered-batch coverage.

These artifacts are useful evidence, but they predate or overlap late patches. The finishing run should create a fresh latest-code artifact before the system is declared complete.

## What Remains

- Add a proper collector validation command, likely a pinned `typescript` and `@types/node` dev dependency plus an npm script.
- Re-run a latest-code full collection with safe page caps and concurrency.
- Decide whether Lee & Associates Buildout failures are transient rate limiting, too-strict abort thresholds, or a source-specific issue.
- Improve limited adapters only where there is a practical public GET or local Firecrawl path. Do not invent coverage for POST-only or gated sources.
- Verify Supabase live after the latest full run: brokerage and transaction counts, recent scrape jobs, soft deletes, and sample `search_cre_listings` results.
- Use `--mark-missing` only after a clean full run satisfies the ingestor floor and error guards.
- Make sure docs match measured behavior after the latest full run.
- Keep generated output and `node_modules` out of git staging.

## Next Best Command Sequence

From repo root:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=savills,nai-global,newmark --transaction=both --max-items=3 --page-cap=5 --concurrency=2 --out=/tmp/cre_probe.json
python3 cre_ingest.py --in /tmp/cre_probe.json --dry-run --keep-artifacts /tmp/cre_probe_ingest
```

Then add the TypeScript validation plumbing and run the full collection only after the probe and dry run are clean.

## 2026-06-11 Final Codex Continuation

New validation and safety plumbing:

- Added pinned TypeScript validation: `npm run typecheck` now runs `tsc --noEmit` with local `typescript` and `@types/node` dev dependencies.
- Added `tsconfig.json` for strict NodeNext checking of `collect.ts`.
- Added `node_modules/`, `out/`, `__pycache__/`, and `*.pyc` to `cre_collector/.gitignore`.
- Improved Buildout handling so Lee/SVN cache source failures and abort when page failures exceed the tolerance instead of producing a gappy run.
- Improved Savills US parsing for state names, ZIP-only rows, and city/state/ZIP variants.
- Documented the service-role-only Supabase access model for the collector-owned `credeals.cre_*` listing surface.

Latest full collection:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=all --transaction=both --max-items=0 --page-cap=400 --concurrency=3 --out=out/full_latest_2026-06-11_230423.json
```

Result:

- Artifact: `out/full_latest_2026-06-11_230423.json`, 41.6 MB.
- Log: `out/full_latest_2026-06-11_230423.log`.
- Wall clock: 27:01.56.
- Run metadata: started `2026-06-12T04:04:23.566Z`, finished `2026-06-12T04:31:24.562Z`.
- Listings: 35,510 raw listing records from 11 source keys, 3,878 unique brokers.
- Per-source raw counts: CBRE 20,684; CBRE Deal Flow 21; JLL 4,678; JLL Investor 50; Cushman 24; Newmark 4,368; Marcus & Millichap 12; Avison Young 22; Savills 100; SVN 5,521; NAI Global 30.
- Unsupported: Colliers and Transwestern remain POST-only/no public GET path in this collector.
- Lee & Associates: failed by design at 12/333 Buildout inventory pages after retries; lease pass reused the cached failure. Do not use this run for Lee reconciliation.

Latest ingest verification:

```bash
python3 cre_ingest.py --in out/full_latest_2026-06-11_230423.json --dry-run --keep-artifacts /tmp/cre_full_latest_2026-06-11_230423_ingest
python3 cre_ingest.py --in out/full_latest_2026-06-11_230423.json --keep-artifacts /tmp/cre_full_latest_2026-06-11_230423_live_ingest
```

Result:

- Dry run staged 33,488 unique upsert rows, 0 skipped for missing URL.
- Live additive ingest staged the same 33,488 rows and completed through `psql`.
- `--mark-missing` was intentionally not used because Lee had source errors.
- Recent `cre_scrape_jobs` rows at `2026-06-12 04:32:01+00` show completed jobs for CBRE, JLL, Cushman, Newmark, Marcus, Avison Young, Savills, SVN, and NAI Global, plus Lee as `partial` with 2 errors.
- Post-ingest active counts: 34,218 active rows in `v_cre_listings_full`, 10,932 in `v_cre_active_for_sale`, 25,531 in `v_cre_active_for_lease`, and 9,883 market summary groups.
- `search_cre_listings('industrial', null, 'TX', null, 'sale')` returned live CBRE Texas candidates.
- Soft deletes remain 0 for this additive run.

Latest validation commands:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
python3 -m py_compile cre_ingest.py
```

All three passed after the latest live ingest. The healthcheck reported expected compose warnings for unset optional env vars, then confirmed API root and scrape smoke success.

Supabase status:

- Project `fhqycqubkkrdgzswccwd` is `supabase-agentic-assets-v2`, `us-east-1`, Postgres 17.6, `ACTIVE_HEALTHY`.
- Collector-owned `cre_*` foreign keys all have covering indexes.
- `search_cre_listings` and `update_cre_listing_timestamp` have fixed `search_path = ''` and are not `SECURITY DEFINER`.
- Base tables and `v_cre_*` views are service-role only for `SELECT`; `anon` and `authenticated` do not have table or view `SELECT`.
- Supabase security and performance advisors include broad project-wide warnings outside this collector. The collector-owned RLS INFO notices are accepted private-schema notices because the listing tables are not publicly granted. psql also reports a project-level collation version warning to handle outside this collector task.

Next daily-run command:

```bash
cd scripts/firecrawl-ops/cre_collector
bash cre_daily_update.sh --no-mark-missing
```

Use the default `bash cre_daily_update.sh` only after a clean all-source run has no Lee/source errors and the per-broker mark-missing guards are acceptable for that day.

## 2026-06-12 Supabase Validation Follow-Up

Saved detailed validation in `VALIDATION_2026-06-12.md`.

Fresh validation found that the latest full artifact uploaded correctly for the rows it safely staged:

- Artifact raw rows: 35,510.
- Dry-run staged rows: 33,488, skipped for missing URL: 0.
- Supabase latest touched rows at `scraped_at='2026-06-12 04:31:24.562+00'`: 33,488.
- Latest scrape jobs: 35,510 discovered, 33,488 saved, 2 errors.
- Active rows: 34,218 in both `cre_listings` and `v_cre_listings_full`.

The 730-row difference between latest touched rows and active rows is older additive inventory, not a latest-upload mismatch. Breakdown: Newmark 715, Marcus & Millichap 6, CBRE 5, Savills 2, SVN 2. Leave `--mark-missing` off while Lee remains unreliable.

Fresh quality checks found no bad URLs, missing titles, bad transaction values, invalid state codes, impossible coordinates, malformed guarded prices, malformed guarded cap rates, missing raw data, duplicate `(brokerage_id, external_id)` groups, or orphan child rows among latest touched rows. NAI Global still has shared `source_url` values because the source exposes cards inside one widget URL.

A Lee-only retry was attempted on 2026-06-12. The endpoint served page 93 when checked alone, but the full run failed under sustained paging at pages 286-297 and aborted with `Error: no listings collected from any source`. No Lee JSON artifact was produced and no Lee rows were ingested.

## 2026-06-12 Cushman & Wakefield Upgrade

Cushman & Wakefield was upgraded after the latest full Supabase ingest. The old rendered Coveo card parser was replaced with the public search API:

```text
https://www.cushmanwakefield.com/api/properties/search?rfkId=property_search&view=pins&site_country=US&listing_type=Buy|Lease&language=en&limit=100&offset=N
```

Verified live source totals during probes:

- Sale: 2,743.
- Lease: 8,575.

The new adapter paginates the API, canonicalizes `sitecore-www.cushmanwakefield.com` detail URLs to `www.cushmanwakefield.com`, scrapes detail pages, reads JSON-LD and markdown facts, and scans raw HTML for asset URLs. This matters because Cushman PDF URLs can appear in raw HTML even when Firecrawl's extracted `links` array omits them.

Targeted proof command:

```bash
CUSHMAN_QUERY='1800 Central' npx tsx collect.ts --source=cushman-wakefield --transaction=sale --max-items=5 --page-cap=5 --concurrency=2 --out=/tmp/cushman_1800_probe.json
```

Result:

- 1 listing, source total 1 for the query.
- URL: `https://www.cushmanwakefield.com/en/united-states/properties/for-sale/office/mo/kansas-city/1800-central/s122093923s122093923-s`.
- Documents: 2 URL rows, the confidentiality agreement and 2026 teaser PDF.
- Photos: 15 URL rows from pmedia group `254906`.
- Contact: Gib Kerr, phone, profile URL, avatar URL, and VCard URL.
- Key facts: 30,000 SF, 0.5 acres, built 1928, sale price text `Contact us for pricing`.

Storage guardrail: document and image collection is URL-only. The collector does not download or upload PDFs/images into Supabase storage.

Validation commands run after the upgrade:

```bash
npm run typecheck
python3 -m compileall -q cre_scrapers
python3 cre_ingest.py --in /tmp/cushman_1800_probe.json --dry-run --keep-artifacts /tmp/cushman_1800_ingest_check
```

Results:

- TypeScript passed.
- Python package compiled.
- Dry-run ingest staged 1 Cushman listing and did not connect to Supabase.
- The targeted artifact had 2 documents, 15 images, 1 contact, and no binary PDF/image fields.

Python `cre_scrapers` reorganization was also completed: broker-specific scraper code now lives under `scripts/firecrawl-ops/cre_scrapers/brokers/<broker>/scraper.py`, each broker folder has a README, and top-level compatibility shims such as `cre_scrapers.cushman` still work.

## 2026-06-12 CBRE Deal Flow Full Run And Ingest

CBRE Deal Flow was upgraded and live-ingested additively after the prior full-run validation.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=cbre-dealflow --transaction=both --max-items=0 --concurrency=4 --out=out/cbre_dealflow_full_2026-06-12_041740.json
python3 cre_ingest.py --in out/cbre_dealflow_full_2026-06-12_041740.json --dry-run --keep-artifacts /tmp/cbre_dealflow_full_2026-06-12_041740_ingest_check
python3 cre_ingest.py --in out/cbre_dealflow_full_2026-06-12_041740.json --keep-artifacts /tmp/cbre_dealflow_full_2026-06-12_041740_live_ingest
```

Result:

- Full artifact: `out/cbre_dealflow_full_2026-06-12_041740.json`, 12.6 MB.
- Full log: `out/cbre_dealflow_full_2026-06-12_041740.log`.
- Collected 1,836 rows: 1,809 sale and 27 lease.
- Public RCM totals reported 2,042 sale and 27 lease. The sale endpoint exposed 1,809 public cards before returning 0 additional cards.
- Dry-run staged 1,836 rows, skipped 0.
- Live ingest completed without `--mark-missing`.
- Active Deal Flow-prefixed rows in Supabase after ingest: 1,857, including earlier additive probe rows.
- Quality checks on the active Deal Flow-prefixed subset: 0 missing URLs, titles, raw data, bad states, bad coordinates, bad cap rates, or child orphans.
- Child rows after ingest: 5,597 contacts, 416 documents, and 40,176 images.
- Sample search proof: `search_cre_listings('industrial', null, 'TX', null, 'sale')` returned a live CBRE Deal Flow row (`Fort Worth Shallow Bay`).

Keep `--mark-missing` off until a clean all-source run has no Buildout or source errors.

## 2026-06-12 Newmark No-State Recovery Ingest

The Newmark Algolia no-state recovery artifact was preserved under `out/` and
live-ingested additively.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
cp /tmp/newmark_no_state_full_probe.json out/newmark_full_2026-06-12_no_state_recovery.json
python3 cre_ingest.py --in out/newmark_full_2026-06-12_no_state_recovery.json --dry-run --keep-artifacts /tmp/newmark_full_2026-06-12_no_state_recovery_ingest_check
python3 cre_ingest.py --in out/newmark_full_2026-06-12_no_state_recovery.json --keep-artifacts /tmp/newmark_full_2026-06-12_no_state_recovery_live_ingest
```

Result:

- Full artifact: `out/newmark_full_2026-06-12_no_state_recovery.json`.
- Collected and staged 4,371 rows, skipped 0.
- Transaction split: 1,121 sale and 3,250 lease.
- Live ingest completed without `--mark-missing`.
- Latest Newmark batch validation: 0 missing URLs, titles, raw data, bad states,
  bad coordinates, bad cap rates, or orphan image rows.
- Latest Newmark batch image rows: 4,303.
- Active Newmark rows after ingest: 5,086 because older additive inventory
  remains active.

Newmark is now public-feed complete for Algolia row coverage, but still needs
deep contact/profile/VCard/document enrichment before it can be called
complete for detail enrichment.
