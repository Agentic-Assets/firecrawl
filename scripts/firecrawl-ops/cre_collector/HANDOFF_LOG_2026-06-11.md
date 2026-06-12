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

Historical note, later source-scoped reconciliations superseded part of this:
the 730-row difference between latest touched rows and active rows was older
additive inventory, not a latest-upload mismatch. The original breakdown
included older Newmark, Marcus & Millichap, CBRE, Savills, and SVN rows. Later
same-day source runs reconciled Marcus, Newmark, Lee, SVN, and other completed
sources. For current counts, use `VALIDATION_2026-06-12.md` and
`START_HERE.md`.

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

## 2026-06-12 Avison Young SharpLaunch Ingest

Avison Young was upgraded from the old shallow rendered-sidebar state to the
full public SharpLaunch active feed and live-ingested additively.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=avison-young --transaction=both --max-items=0 --concurrency=4 --out=out/avison_full_2026-06-12_043342.json
python3 cre_ingest.py --in out/avison_full_2026-06-12_043342.json --dry-run --keep-artifacts /tmp/avison_full_2026-06-12_043342_ingest_check
python3 cre_ingest.py --in out/avison_full_2026-06-12_043342.json --keep-artifacts /tmp/avison_full_2026-06-12_043342_live_ingest
```

Result:

- Full artifact: `out/avison_full_2026-06-12_043342.json`, 6.4 MB.
- Full log: `out/avison_full_2026-06-12_043342.log`.
- Raw rows: 2,333.
- Staged unique rows: 2,200, skipped 0.
- Active Supabase split: 636 sale, 1,431 lease, and 133 `sale_or_lease`.
- Latest-batch quality checks: 0 missing URLs, titles, raw data, bad states,
  bad coordinates, bad cap rates, or orphan contact/image rows.
- Latest-batch child rows: 4,125 contacts and 2,186 images.

The SharpLaunch public feed is loaded. Bounded detail enrichment is now
implemented and verified for selected rows, but full-feed detail enrichment has
not been live-run.

Bounded detail proof:

- Command:
  `npx tsx collect.ts --source=avison-young --transaction=both --max-items=2 --concurrency=2 --out=/tmp/avison_young_detail_probe_after_ingest_filter_2026-06-12.json`.
- Result: 4 listings, 6 public PDF document URLs, 36 public image URLs, 5
  contact rows, 1 broker profile URL, 0 VCards, 4 JSON-LD payloads, and 0
  detail errors.
- Dry-run ingest staged all 4 rows and skipped 0 missing URLs.
- A later full-feed dry-run after the bounded detail patch stayed
  SharpLaunch-only by default and staged 2,199 unique rows from 2,332 raw rows.
  No live Avison reconciliation was run from that drifted probe.

## 2026-06-12 Child URL Filter And Bad Avatar Cleanup

The ingestor now drops non-HTTP child URLs for contact profile/avatar/VCard
fields and document URLs before staging. This was verified by dry-running and
then live-refreshing child rows from the already complete Lee and SVN artifacts
without `--mark-missing`.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 cre_ingest.py --in out/lee_full_cache_2026-06-12_assembled.json --dry-run --keep-artifacts /tmp/lee_avatar_filter_reingest_dry
python3 cre_ingest.py --in out/svn_full_cache_2026-06-12_assembled.json --dry-run --keep-artifacts /tmp/svn_avatar_filter_reingest_dry
python3 cre_ingest.py --in out/lee_full_cache_2026-06-12_assembled.json --keep-artifacts /tmp/lee_avatar_filter_reingest_live
python3 cre_ingest.py --in out/svn_full_cache_2026-06-12_assembled.json --keep-artifacts /tmp/svn_avatar_filter_reingest_live
npm run validate:supabase -- --out /tmp/cre_validate_after_avatar_filter_2026-06-12.md
```

Result:

- Lee dry-run staged 9,223 rows and skipped 0 missing URLs.
- SVN dry-run staged 5,287 rows and skipped 0 missing URLs.
- Active bad contact avatar URLs went from 37 to 0.

## 2026-06-12 Folded Source Mark-Missing Guard

The ingestor now refuses parent-level `--mark-missing` when a folded source
batch is incomplete. This prevents a `cbre-dealflow` only artifact from
soft-deleting main `cbre` rows, and likewise prevents a `jll` only artifact
from soft-deleting `jll-investor` rows.

Verification:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 cre_ingest.py --in out/cbre_dealflow_full_2026-06-12_041740.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/cbre_dealflow_mark_missing_guard_check_2026-06-12
```

Result: 1,836 rows staged, 0 skipped missing URLs, `cbre` mark-missing skipped
because the batch saw only `cbre-dealflow`, and the generated SQL had no
parent-level soft-delete block for `cbre`.

## 2026-06-12 JLL Investor Sitemap Detail Probe

JLL Investor Center now has an implemented public sitemap/detail path scoped to
source key `jll-investor`. It avoids the robots-disallowed query-string
pagination route, discovers detail URLs from `https://invest.jll.com/us/sitemap-us.xml`,
parses public detail-page `__NEXT_DATA__`, and retains only detail rows whose
country normalizes to `US`.

Verification:

```bash
cd scripts/firecrawl-ops/cre_collector
JLL_INVESTOR_SITEMAP_SCAN_LIMIT=8 npx tsx collect.ts --source=jll-investor --transaction=sale --max-items=4 --concurrency=2 --out=/tmp/jll_investor_sitemap_probe_review_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_investor_sitemap_probe_review_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_investor_sitemap_probe_review_ingest_2026-06-12
```

Result:

- Latest current-tree probe found 1,855 sitemap detail URLs.
- Scanned 8 detail URLs, retained 3 U.S. rows, and saw 0 detail errors.
- Output contained 3 public document URLs, 15 image URLs, 6 contacts, and only
  `US` countries.
- Dry-run ingest staged 3 `jll-investor` rows and skipped 0 missing URLs.
- URL-only SQL sanity found no `data:` or `base64` strings.
- No live JLL Investor ingest was run.

## 2026-06-12 Colliers SalesTracker Partial Adapter

Colliers was upgraded from fully unsupported to partial investment-sale support
through the public SalesTracker RCM GET path. Main
`www.colliers.com/en/properties` sale and lease coverage remains blocked behind
the Coveo POST workflow.

Implemented in `collect.ts`:

- `srcColliers()` uses `https://sales.colliers.com/` to extract the RCM engine
  key.
- Listing cards come from
  `https://my.rcm1.com/api/AjaxEngine/GetListingsHtml?...`.
- Coordinates and stable `ProjectId` values come from
  `https://my.rcm1.com/api/AjaxEngine/GetMapData?...`.
- Public detail enrichment uses
  `https://my.rcm1.com/api/handler/slp/Init?pv=<public-card-detail-pv>`.
- Lease mode returns an explicit zero-row source entry because no Colliers lease
  GET feed is proven.
- Brochure and agreement URLs are retained only in raw metadata. The collector
  does not download PDFs/images or classify gated document paths as public
  document rows.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=colliers --transaction=sale --max-items=3 --page-cap=1 --concurrency=2 --out=/tmp/colliers_collector_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/colliers_collector_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/colliers_collector_probe_2026-06-12_ingest
npx tsx collect.ts --source=colliers --transaction=both --max-items=3 --page-cap=1 --concurrency=2 --out=/tmp/colliers_collector_both_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/colliers_collector_both_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/colliers_collector_both_probe_2026-06-12_ingest
python3 -m py_compile cre_ingest.py
```

Results:

- Direct public GET probe artifacts are in
  `/tmp/colliers_probe_2026-06-12_codex/`.
- SalesTracker reported `total=1653` and `totalAvail=2094`.
- Targeted collector probe collected ProjectIds `150540`, `150534`, and
  `150533`.
- Dry-run ingest staged 3 Colliers rows and skipped 0 rows for missing URL.
- The probe had 4 public contacts, 9 image URLs, 0 document rows, and 0
  `detailError` rows.
- One card had no public SLP detail link and was retained as a card/map row.

Follow-up full run and ingest:

```bash
npx tsx collect.ts --source=colliers --transaction=both --max-items=0 --page-cap=30 --concurrency=2 --out=out/colliers_salestracker_full_2026-06-12_050241.json
python3 cre_ingest.py --in out/colliers_salestracker_full_2026-06-12_050241.json --dry-run --keep-artifacts /tmp/colliers_salestracker_full_2026-06-12_050241_ingest_check
python3 cre_ingest.py --in out/colliers_salestracker_full_2026-06-12_050241.json --keep-artifacts /tmp/colliers_salestracker_full_2026-06-12_050241_live_ingest
```

Result:

- Public SalesTracker pages exposed 1,300 unique sale cards before a 0-card
  page, while RCM reported `total=1653` and `totalAvail=2094`.
- Artifact detail coverage: 1,207 unique brokers, 2,915 contact rows, 10,036
  image URLs, 0 document rows, 0 missing URLs, 0 missing titles, and 0
  `detailError` rows.
- 486 cards lacked public SLP detail links and were retained as card/map rows.
- 128 repeated ProjectId groups in public cards deduped to 1,172 staged rows.
- Live additive ingest completed without `--mark-missing`.
- Supabase proof after ingest: 1,172 active Colliers rows, 2,733 contact child
  rows, 9,908 image child rows, 0 missing URLs, 0 missing titles, 0 missing raw
  data, 0 bad states, 0 impossible coordinates, 0 duplicate external IDs, and
  0 orphan contacts/documents/images.
- `search_cre_listings('office', null, null, null, 'sale')` returned live
  Colliers rows.

Remaining limit: this is only SalesTracker investment-sale coverage. Main
Colliers Coveo sale/lease coverage remains blocked.

## 2026-06-12 NAI Global Infabode Active-Status Filter And Ingest

NAI Global was upgraded from rendered widget-card probes to the public Infabode
GraphQL feed and `publicPost(id)` detail path.

Public paths:

- Widget: `https://ab.infabode.com/nai-global/listings3`
- Feed: `POST https://infabode.com/public_api`
- Detail: `POST https://infabode.com/graphql`, query `publicPost(id: Int!)`
- Listing URL: `https://infabode.com/services/listings/<id>`

Important policy finding:

- The public feed pages back to 2021 and does not expose a server-side active
  status filter.
- The unbounded public artifact collected 13,597 rows, but only 241 had
  `publicPost.listingStatus` containing `FOR_SALE_ON_MARKET`.
- Rows with `UNKNOWN`, `SOLD`, `UNDER_OFFER`, null status, or detail failures
  are public historical or ambiguous records, not defensible active inventory.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=nai-global --transaction=both --max-items=12 --page-cap=4 --concurrency=2 --out=out/nai_status_probe_2026-06-12.json
npx tsx collect.ts --source=nai-global --transaction=both --max-items=24 --page-cap=6 --concurrency=2 --out=out/nai_active_filter_probe_2026-06-12.json
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --dry-run --keep-artifacts /tmp/nai_active_only_2026-06-12_ingest_check
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --keep-artifacts /tmp/nai_active_only_2026-06-12_live_ingest
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/nai_active_only_2026-06-12_mark_missing_check
python3 cre_ingest.py --in out/nai_active_only_from_full_2026-06-12_044310.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/nai_active_only_2026-06-12_mark_missing_live
```

Result:

- Filtered active artifact:
  `out/nai_active_only_from_full_2026-06-12_044310.json`.
- Active rows retained: 241 total, 183 sale and 58 lease, all 2026-dated and all
  `FOR_SALE_ON_MARKET`.
- Dry-run staged 241 rows and skipped 0 missing URLs.
- Live ingest completed, then source-scoped `--mark-missing` soft-deleted 19
  old rendered-card probe rows under brokerage slug `nai-global`.
- Supabase proof: 241 active NAI Global rows, 670 image URL child rows, 1
  document URL child row, 0 contact rows, 0 missing URLs, 0 missing titles, 0
  missing raw data, 0 non-active statuses, 0 duplicate external IDs, 0 bad
  states, 0 bad coordinates, and 0 child orphans.

Remaining limit: the public API did not expose broker names, phones, profile
URLs, or VCards for sampled fields. Do not treat the historical public Infabode
rows as active unless EQUIRE adds a separate archive/history surface.

## 2026-06-12 Cushman & Wakefield Full API Ingest

Cushman & Wakefield was completed from the public search API and detail-page
enrichment path.

Command:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=cushman-wakefield --transaction=both --max-items=0 --page-cap=400 --concurrency=6 --out=out/cushman_full_2026-06-12_022841.json
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --dry-run --keep-artifacts /tmp/cushman_full_2026-06-12_022841_ingest_check
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/cushman_full_2026-06-12_022841_mark_missing_check
python3 cre_ingest.py --in out/cushman_full_2026-06-12_022841.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/cushman_full_2026-06-12_022841_mark_missing_live
```

Result:

- Full artifact: `out/cushman_full_2026-06-12_022841.json`, 43.2 MB.
- Runtime: 4:41:00.
- Collected rows: 11,318 total, 2,743 sale and 8,575 lease.
- Source totals matched collected rows for both sale and lease.
- Detail coverage: 18,343 document URLs, 24,278 image URLs, 21,110 detailed
  contacts, 21,110 profile URLs, 20,301 VCard URLs, and 0 detail errors.
- Dry-run staged 11,318 rows and skipped 0 missing URLs.
- Source-scoped `--mark-missing` was dry-run and then applied only for
  `cushman-wakefield`.
- Supabase proof after ingest: 11,318 active Cushman rows, 2,743 sale and 8,575
  lease, 24,278 image child rows, 18,343 document child rows, 21,110 contact
  child rows, 0 missing URLs, 0 missing titles, 0 missing raw data, 0 duplicate
  external IDs, 0 bad states, 0 impossible coordinates, 0 malformed guarded
  prices/cap rates, and 0 orphan contacts/documents/images.
- Old shallow/probe rows soft-deleted: 24.

Remaining limit: none for public feed coverage. Treat future Cushman work as
field enrichment audit only.

## 2026-06-12 Transwestern Full GET Feed Ingest

Transwestern was completed from the public GET feed and detail-page enrichment
path after a targeted description-cleanup guard.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=transwestern --transaction=both --max-items=0 --concurrency=4 --out=out/transwestern_full_2026-06-12_121302.json
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --dry-run --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_ingest_check
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_mark_missing_check
python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302_cleaned.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/transwestern_full_2026-06-12_121302_cleaned_mark_missing_live_retry
```

Result:

- Raw artifact: `out/transwestern_full_2026-06-12_121302.json`.
- Cleaned artifact: `out/transwestern_full_2026-06-12_121302_cleaned.json`.
- Raw collection: 2,151 rows, 289 unique run-level brokers, 519 sale-bucket
  rows, and 1,632 lease-bucket rows.
- `Sale or Lease` rows appeared in both passes by design. Dry-run staged 2,021
  unique rows and skipped 0 missing URLs.
- Detail coverage in the full artifact: 3,184 document URLs, 5,093 image URLs,
  3,963 contacts/profile URLs/VCard URLs, and 0 detail errors.
- The cleaned artifact removed 2,151 footer/TREC/copyright descriptions after a
  performance review found they were site boilerplate, not property narratives.
- Live database was missing the already-defined `transwestern` brokerage seed
  row. Inserted the seed, then reran the same live ingest.
- Live source-scoped `--mark-missing` completed for `transwestern`.
- Supabase proof: 2,021 active Transwestern rows, 389 sale, 1,502 lease, 130
  sale_or_lease, 4,838 image child rows, 3,054 document child rows, 3,746 contact
  child rows, 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad
  descriptions, 0 duplicate external IDs, 0 bad states, 0 impossible
  coordinates, 0 malformed guarded prices/cap rates, 0 bad asset URLs, and 0
  child orphans.
- Search proof: `search_cre_listings('National Avenue', null, null, null, null)`
  returned the live Transwestern `1025 W. National Avenue` row.

Remaining refinements: add a detail cache to avoid double-scraping the 130
sale-or-lease rows, harden availability-table parsing, and promote clearly valid
detail prices/rates where the feed has zero price.

## 2026-06-12 Lee Buildout Throttling Finding (Superseded By Cache Assembly)

Historical note: Lee was not uploaded at this point in the day. A side-agent
probe found that individual Buildout pages were healthy, including pages `0`,
`32`, `286`, `297`, and `332`, and the current Lee total was `9972`. The
blocker was sustained full-inventory behavior: after many pages, Buildout
returned temporary 403 HTML or non-JSON responses. This was later resolved by
the durable cache assembly documented in the Lee completion section below.

Saved note:
`cre_scrapers/brokers/lee_associates/LEE_BUILDOUT_THROTTLING_RESUMABILITY_2026-06-12.md`.

Next safe plan: add opt-in durable page cache, true page-window cache-fill
controls, pacing before fallback, and attempted/failed/unattempted diagnostics;
then fill small windows before assembling a Lee-only no-ingest full artifact.

## 2026-06-12 JLL/Newmark Detail Review (Newmark Partly Superseded)

JLL remains loaded but not detail-complete. The Newmark contact/state findings
in this review were later acted on in the refined Newmark completion section
below. A side-agent review saved notes under the broker folders:

- `cre_scrapers/brokers/jll/PERFORMANCE_ACCURACY_REVIEW_2026-06-12.md`
- `cre_scrapers/brokers/newmark/PERFORMANCE_ACCURACY_REVIEW_2026-06-12.md`

Highlights:

- JLL now covers all nine property-type filters across sale and lease, but main
  rows are still card-level. Detail-page `__NEXT_DATA__` artifacts show
  brochures, images, brokers, coordinates, and profile-like broker fields are
  available. No VCard URLs were observed.
- Newmark was feed-complete via Algolia after no-state recovery but not
  deep-contact complete at this point. This was later refined with cached
  Algolia People lookup by exact `broker_name`, not noisy detail-page shells,
  and the Washington, DC rows now carry `state: DC`.

## 2026-06-12 Marcus & Millichap Full Public Sale Ingest

Marcus & Millichap is now live-ingested and validated for the defensible public
sale feed. Public lease remains blocked because no public lease UI mode or
endpoint has been proven.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=marcus-millichap --transaction=both --max-items=0 --concurrency=6 --out=out/marcus_full_2026-06-12_130035.json
python3 cre_ingest.py --in out/marcus_full_2026-06-12_130035.json --dry-run --keep-artifacts /tmp/marcus_full_2026-06-12_130035_ingest_check
python3 cre_ingest.py --in out/marcus_full_2026-06-12_130035.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/marcus_full_2026-06-12_130035_mark_missing_check
python3 cre_ingest.py --in out/marcus_full_2026-06-12_130035.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/marcus_full_2026-06-12_130035_mark_missing_live
```

Collector notes:

- Added a Marcus-only detail JSONL checkpoint beside the output artifact.
- The checkpoint recovered the interrupted full run pattern and made the retry
  run reuse 3,119 successful detail rows while retrying five transient
  `fetch failed` rows.
- Cache reads now skip rows with `detailError`, and cache writes no longer append
  failed detail rows.

Result:

- Artifact: `out/marcus_full_2026-06-12_130035.json`, 15.5 MB.
- Detail cache: `out/marcus_full_2026-06-12_130035.json.marcus-detail-cache.jsonl`.
- Collected rows: 3,124 sale rows and 0 lease rows.
- Artifact QA: 0 missing URLs, 0 missing titles, 0 duplicate IDs, 0 final detail
  errors, 16,771 image URLs, 7,915 contact/profile URL rows, and 3,124 gated
  deal-room URLs kept only in raw metadata.
- Dry-run ingest staged 3,124 rows and skipped 0 missing URLs.
- Source-scoped `--mark-missing` was dry-run and then live-applied only for
  `marcus-millichap`.
- Supabase proof: 3,124 active Marcus rows, all sale; 16,771 image child rows;
  7,915 contact child rows; 0 document rows; 0 bad source URLs, missing titles,
  missing raw data, duplicate external IDs, bad states, impossible coordinates,
  malformed guarded prices/cap rates, bad child URLs, or child orphans.
- Search proof: the UI-updated five-argument
  `credeals.search_cre_listings(query, p_city, p_state, p_type, p_transaction)`
  returned live Marcus rows for query `marcus`.

Ops note:

- `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh` failed during this pass
  because the OrbStack Docker socket was unavailable and `localhost:3002` was
  not accepting connections. Marcus uses direct public HTTP endpoints, so the
  Marcus run and ingest were not blocked by local Firecrawl being down.

## 2026-06-12 Lee & Associates Full Buildout Ingest

Lee & Associates is now live-ingested and validated for the public Buildout
inventory feed. This did not use local Firecrawl because the durable page-cache
fill used direct public Buildout JSON successfully.

Code change:

- Added durable Buildout page-cache controls in `collect.ts`.
- Lee opts into `cacheSlug: "lee-associates"` and `usePageCache: true`.
- `BUILDOUT_CACHE_ONLY=1` fills a page window and then refuses to emit a partial
  artifact.
- `BUILDOUT_ASSEMBLE_FROM_CACHE=1` disables recovery/network fallback and fails
  if any expected page is missing.
- `BUILDOUT_PAGE_START`, `BUILDOUT_PAGE_END`, and `BUILDOUT_PAGE_JITTER_MS`
  control windowed cache fills.

Cache-fill proof:

- Cached pages: 333 of 333, contiguous pages 0 through 332.
- Cache location: `out/cache/buildout/lee-associates/` (gitignored).
- Cache size: about 37 MB.
- Page 0 and page 332 both reported `total=9975`, `limit=30`; page 332 had 15
  inventory rows.
- Old failure window pages 286 through 297 were filled successfully.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
BUILDOUT_ASSEMBLE_FROM_CACHE=1 npx tsx collect.ts --source=lee-associates --transaction=both --max-items=0 --concurrency=1 --out=out/lee_full_cache_2026-06-12_assembled.json
python3 cre_ingest.py --in out/lee_full_cache_2026-06-12_assembled.json --dry-run --keep-artifacts /tmp/lee_full_cache_2026-06-12_assembled_ingest_check
python3 cre_ingest.py --in out/lee_full_cache_2026-06-12_assembled.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/lee_full_cache_2026-06-12_assembled_mark_missing_check
python3 cre_ingest.py --in out/lee_full_cache_2026-06-12_assembled.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/lee_full_cache_2026-06-12_assembled_mark_missing_live
```

Result:

- Artifact: `out/lee_full_cache_2026-06-12_assembled.json`, 8.9 MB.
- Raw rows: 9,975.
- Sale rows before merge: 3,447.
- Lease rows before merge: 6,528.
- Unique run-level brokers: 1,085.
- Artifact QA: 0 missing URLs, 0 missing titles, 0 closed rows, 8,238 document
  URLs, 9,975 image URLs, and 9,975 broker references.
- Dry-run staged 9,223 unique rows and skipped 0 missing URLs.
- Expected merge explanation: 744 sale+lease property pairs merged to
  `sale_or_lease` after stripping Buildout `propertyId` `-sale`/`-lease`
  suffixes, plus 8 exact duplicate rows.
- Source-scoped `--mark-missing` was dry-run and then live-applied only for
  `lee-associates`.
- Supabase proof: 9,223 active Lee rows, 2,611 sale, 5,691 lease, 921
  sale_or_lease, 9,062 image child rows, 7,681 document child rows, 9,223
  contact child rows, and 0 bad source URLs, missing titles, missing raw data,
  duplicate external IDs, bad states, impossible coordinates, malformed guarded
  prices/cap rates, bad child URLs, or child orphans.
- Search proof: `credeals.search_cre_listings('Lee', null, null, null, null)`
  returned live Lee rows.

## 2026-06-12 Newmark Refined Public Feed Ingest

Newmark is now live-ingested and validated for the defensible public Algolia
listing feed, with state recovery and first-broker public People enrichment.
Listing documents, full galleries, second/third broker joins, and VCards remain
unproven.

Code change:

- Newmark listing Algolia calls now use direct public JSON once the public page
  has exposed the app id/search key/index name.
- Credential extraction retries the rendered page with longer wait before
  failing.
- The mapper preserves `rawNewmarkHit` and `newmarkBrokerProvenance`.
- Missing Washington, DC state is inferred only for city `Washington` and ZIPs
  beginning with `200`.
- First-broker contacts are enriched through cached Algolia People lookups by
  exact normalized public `broker_name`, with absolute `nmrk.com` profile URLs.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=newmark --transaction=both --max-items=20 --concurrency=3 --out=/tmp/newmark_refinement_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/newmark_refinement_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/newmark_refinement_probe_2026-06-12_ingest_check
npx tsx collect.ts --source=newmark --transaction=both --max-items=0 --concurrency=4 --out=out/newmark_full_refined_2026-06-12.json
python3 cre_ingest.py --in out/newmark_full_refined_2026-06-12.json --dry-run --keep-artifacts /tmp/newmark_full_refined_2026-06-12_ingest_check
python3 cre_ingest.py --in out/newmark_full_refined_2026-06-12.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/newmark_full_refined_2026-06-12_mark_missing_check
python3 cre_ingest.py --in out/newmark_full_refined_2026-06-12.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/newmark_full_refined_2026-06-12_mark_missing_live
```

Result:

- Artifact: `out/newmark_full_refined_2026-06-12.json`, 22.0 MB.
- Raw rows: 4,371, with 1,121 sale and 3,250 lease.
- Artifact QA: 0 missing URLs, 0 missing titles, 0 missing states, 0 duplicate
  IDs, 4,303 image URLs, 3,961 contact/profile URL rows, and 0 document URLs.
- Dry-run ingest staged 4,371 rows and skipped 0 missing URLs.
- Source-scoped `--mark-missing` was dry-run and then live-applied only for
  `newmark`, soft-deleting 715 older additive rows.
- Supabase proof: 4,371 active Newmark rows, 1,121 sale, 3,250 lease, 4,303
  image child rows, 3,961 contact child rows with profile URLs, and 0 bad source
  URLs, missing titles, missing raw data, invalid states, impossible
  coordinates, malformed guarded prices/cap rates, duplicate external IDs, bad
  child URLs, or child orphans.
- Search proof: `credeals.search_cre_listings('Alvista', null, null, null,
  null)` returned the live Newmark `Alvista Sterling Palms` row.

## 2026-06-12 JLL Detail Enrichment Cache Probe

Main JLL is still pending a full detail-enriched collection, but the collector
now has the detail parser and resumable cache needed for that full run.

Code change:

- `srcJll` still discovers rendered public `property.jll.com` search pages
  across all documented property-type filters.
- Each selected listing URL is enriched from detail-page `script#__NEXT_DATA__`
  using `pageProps.property` and `pageProps.brokers`.
- Detail pages are cached under `out/cache/jll-detail/` by normalized listing
  URL so interrupted full runs can resume without losing completed detail
  scrapes.
- Per-row `detailError` is retained instead of failing the source.
- Public URL-only assets include brochures, floor plans, images, broker profile
  URLs, avatar URLs, and LinkedIn URLs. No binaries are downloaded.

Verification:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=jll --transaction=both --max-items=6 --page-cap=1 --concurrency=2 --out=/tmp/jll_detail_cached_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_detail_cached_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_detail_cached_probe_2026-06-12_ingest_check
```

Probe result:

- 12 listings emitted: 6 sale and 6 lease.
- 0 detail errors, 0 skipped ingest rows, and 0 duplicate collector ids.
- 12 rows had stable JLL property ids and coordinates.
- 10 public document URLs, 37 image URLs, 25 contact rows, and 25 broker
  profile URLs were emitted.
- The JLL detail cache contained 12 rendered detail files after the probe.
- A first full-run attempt was stopped before ingest because sale/industrial
  rendered 0 cards despite prior evidence of 492 rows. The collector now retries
  zero-card search pages with longer waits before accepting them.
- Retry verification on `--transaction=sale --max-items=12 --page-cap=1`
  covered all nine property-type tokens, including industrial, with 0 detail
  errors, 12 stable ids, 12 public document URLs, 27 contact rows, and 0 skipped
  ingest rows.

Next step: run full JLL with the cache enabled, dry-run ingest, inspect
detail-error and merge counts, then live ingest with source-scoped
`--mark-missing` only if the full run is clean and current.

## 2026-06-12 SVN Buildout Cache Path Reopened

A focused SVN worker verified that SVN can use the Lee-style durable Buildout
page cache path. No Supabase ingest was run and no binaries were downloaded.

Code change:

- SVN now passes `cacheSlug: "svn"` and `usePageCache: true` into the shared
  Buildout inventory helper.
- SVN recovery remains conservative with `pageConcurrency: 1`,
  `requireCompletePages: true`, `recoveryPasses: 1`, and `maxRecoveryPages: 60`.

Probe result:

- `BUILDOUT_CACHE_ONLY=1` for pages 0 through 2 succeeded against public
  Buildout JSON and correctly refused to write a partial listing artifact.
- `BUILDOUT_ASSEMBLE_FROM_CACHE=1` failed closed when only pages 0 through 2
  were present, reporting missing pages beginning at page 3.
- A follow-up cache window filled pages 3 through 4.
- SVN cache now has pages 0 through 4 under gitignored
  `out/cache/buildout/svn/`, each reporting `total=5526`, `limit=30`, and
  `rows=30`.
- `npm run typecheck` and `python3 -m py_compile cre_ingest.py` passed.

Next safe SVN pattern:

```bash
cd scripts/firecrawl-ops/cre_collector
BUILDOUT_CACHE_ONLY=1 BUILDOUT_PAGE_START=5 BUILDOUT_PAGE_END=24 \
  BUILDOUT_PAGE_JITTER_MS=250,1000 npx tsx collect.ts \
  --source=svn --transaction=sale --max-items=0 --concurrency=1 \
  --out=/tmp/svn_cache_window_should_not_write.json
```

Repeat windows until pages 0 through 184 are present, then assemble only from
cache and dry-run ingest before any live upload or source-scoped reconciliation.

## 2026-06-12 SVN Full Cache Assembly And Live Ingest

SVN is now complete for its public Buildout feed. The durable cache was filled
through page 184, assembled from cache only, dry-run staged cleanly, and then
live-ingested with source-scoped reconciliation.

Commands:

```bash
cd scripts/firecrawl-ops/cre_collector
BUILDOUT_ASSEMBLE_FROM_CACHE=1 FIRECRAWL_API_URL=http://localhost:3002 npx tsx collect.ts --source=svn --transaction=both --max-items=0 --concurrency=1 --out=out/svn_full_cache_2026-06-12_assembled.json
python3 cre_ingest.py --in out/svn_full_cache_2026-06-12_assembled.json --dry-run --keep-artifacts /tmp/svn_full_cache_2026-06-12_assembled_ingest_check
python3 cre_ingest.py --in out/svn_full_cache_2026-06-12_assembled.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/svn_full_cache_2026-06-12_assembled_mark_missing_check
python3 cre_ingest.py --in out/svn_full_cache_2026-06-12_assembled.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/svn_full_cache_2026-06-12_assembled_mark_missing_live
```

Result:

- Artifact: `out/svn_full_cache_2026-06-12_assembled.json`, 5.2 MB.
- Raw collected rows: 5,526, with 2,989 sale-bucket rows and 2,537 lease-bucket
  rows.
- Artifact URL-only coverage: 4,071 document rows, 5,526 image rows, 5,526
  broker/contact refs, and 636 unique run-level brokers.
- Dry-run staged 5,287 unique rows and skipped 0 missing URLs.
- Source-scoped live reconciliation soft-deleted 34 old SVN rows.
- Active Supabase rows after ingest: 5,287, with 2,660 sale, 2,192 lease, and
  435 sale_or_lease.
- Active child rows: 5,235 image URLs, 3,899 document URLs, and 5,287 contacts.
- Validation found 0 duplicate external IDs, bad URLs, missing titles, missing
  raw data, invalid states, impossible coordinates, malformed guarded
  prices/cap rates, bad child URLs, or child orphans.
- One active SVN row is missing state.
- Search proof:
  `credeals.search_cre_listings('1500', null, null, null, null)` returned live
  SVN rows.

## 2026-06-12 Savills Commercial Lease Recheck

Savills remains partial, but the collector now has a defensible public U.S.
commercial lease path. Sale remains not CRE-defensible.

Code change:

- Savills lease now parses the public server-rendered commercial lease page:
  `https://search.savills.com/com/en/list/commercial/property-to-let/united-states-of-america`.
- The parser reads public `__NEXT_DATA__` property objects and emits URL-only
  PDFs, images, and visible contacts.
- Savills location parsing was tightened so `Chicago IL` stages as city
  `Chicago`, state `IL`.

Verification:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=savills --transaction=lease --max-items=0 --page-cap=5 --concurrency=1 --out=/tmp/savills_lease_main_verify_2026-06-12.json
python3 cre_ingest.py --in /tmp/savills_lease_main_verify_2026-06-12.json --dry-run --keep-artifacts /tmp/savills_lease_main_verify_2026-06-12_ingest_check
```

Result:

- 2 U.S. commercial lease rows, both Chicago, IL retail listings.
- 4 public PDF document URLs, 24 image URLs, and 2 contact rows.
- Dry-run ingest staged both rows and skipped 0 missing URLs.
- A later additive live ingest was run from
  `out/savills_lease_public_2026-06-12_live_candidate.json` without
  `--mark-missing`. Live Savills now has 104 active rows, 101 sale and 3 lease,
  with 4 document URL rows, 31 image URL rows, and 104 contact rows.

Limit:

- The current Savills sale path still comes from a global/residential search
  and must not be treated as complete CRE sale coverage. The corrected public
  commercial sale route exposed only a Toronto, Canada sale object during the
  recheck.
