# CRE Collector Start Here

> **Current ownership, 2026-07-11:** GetCREdata is the sole production OM
> extraction writer. `om_parse.py --apply` fails closed with exit `78`, and
> `cre_enrich.py` has no OM-parse invocation. The dated snapshots below are
> historical records, not authorization to reactivate that writer.
>
> **Current operational handoff:** Before any merge, Mac mini recovery,
> scheduler change, or bounded canary, read
> `../../../tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md`.
> It is the current ordered runbook. The historical run counts and scheduler
> states below must not be used as current live evidence.

> **Supported production path:** When collection and ingest are enabled, they
> run here (`collect.ts`, `sources/*.ts`, `cre_ingest.py`, and
> `cre_daily_update.sh`). `../cre_scrapers/brokers/` is legacy reference code,
> not a source of board updates.

## Agent rule: verify counts before you quote or edit them

Board totals, source counts, test counts, and scheduler state are point-in-time
evidence. Before quoting them in docs, commits, or handoffs, run the relevant
read-only preflight and test commands, then label any refreshed snapshot with
its date. Never treat a historical launchd statement as live proof.

```bash
cd scripts/firecrawl-ops/cre_collector
bash cre_status.sh                    # read-only scheduler and marker preflight
python3 -m pytest tests/ -q           # obtain the current pytest count
npm run test:unit                     # obtain the current TypeScript count
```

## Strict listing refresh

Use `cre_checkpoint_refresh.py` for an operator-requested full refresh. It
collects, validates, gates, and dry-runs each source into a resumable manifest.
It stops on the first non-`ok` source gate, runs one aggregate gate over the
complete prepared artifact set, and only then begins additive live ingest. The
monolithic `cre_daily_update.sh` remains the scheduled backstop, not the proof
path for a source-fresh detail sweep.

```bash
python3 cre_checkpoint_refresh.py \
  --env-file "$HOME/.config/cre/equire.env"

# Resume the exact run after interruption:
python3 cre_checkpoint_refresh.py \
  --resume out/checkpoint-refresh/<run-id> \
  --env-file "$HOME/.config/cre/equire.env"
```

The strict runner never passes `--monitor`, `--mark-missing`,
`--activate-status`, or `--update-baseline`. It uses run-scoped cache
generations for JLL, Colliers Main, Buildout, and Marcus; forces full Cushman
detail mode; and enables Avison Young detail collection. A `first_seen` verdict
stops at `baseline_seed_required`; a `hold` stops at `gate_blocked`. Neither
state reaches dry-run or live ingest. Seed a first baseline only after reviewing
the complete exact artifact with `cre_gate.py --apply --update-baseline`, read
it back without `--update-baseline`, and then resume the same immutable run.
The manifest records `ingesting` before launching a live write. If execution
stops in that window, resume performs an exact database readback and never
automatically replays an ambiguous ingest.

“All” means the 20 source keys in the current TypeScript collector registry.
Older active rows whose brokerage is outside that registry are reported by
`cre_refresh_report.py`, but this runner cannot make those legacy rows
source-fresh. Add or restore a supported adapter before claiming full-database
source coverage.

Accordingly, a successful manifest is labeled
`supported_scope_complete`, not full-database complete.

After the run, create a date-bounded database readback from the exact run start:

```bash
python3 cre_refresh_report.py \
  --since <run-start-utc> \
  --env-file "$HOME/.config/cre/equire.env" \
  --format markdown \
  --out /tmp/cre-refresh-report.md
```

`scraped_at` proves that a row was re-observed and ingested. A strict
detail-freshness claim additionally requires the run manifest, per-source cache
generation, zero source errors/truncation, and explicit handling of any
listing-level `detailError`.

**2026-06-15 (Phase-2 data-lift LIVE).** DDL `011` -> `012` -> `013` -> `014`
applied to prod (project `fhqycqubkkrdgzswccwd`, schema `credeals`) in order via
psql (non-pooling, `ON_ERROR_STOP`): `011` added `cre_listing_media` +
`cre_listing_links` (+ archive mirrors) and the widening
`cre_listing_documents.doc_type` CHECK rebuild (adds `financials`, `rent_roll`);
`012` added institutional + geo scalar columns (incl. `cbsa_code`, `cbsa_name`,
`geo_source`, `extra_facts`) on `cre_listings` plus `license` on
`cre_listing_contacts`; `013` added `cre_listing_om_facts`; `014` added the
`cre_zip_cbsa_crosswalk` reference table. `cre_zip_cbsa_crosswalk` LOADED (33,791
rows; 24,734 with a CBSA, 0 NULL centroids). The three additive backfills then
ran (all COALESCE-keep, status never touched): `cre_backfill_raw_data.py --apply`
filled active-row coverage canonical_url 0 -> 87,324, cap_rate 2,235, submarket
12,465, building_class 9,138, property_subtype 8,330, year_built 13,031, plus
M&M tenant_name 823 / guarantor 833 / grm 624 (0 decode failures on all 87,328
rows); `om_classify_existing.py --apply` upgraded 14,087 of 70,414 brochure rows
(flyer 11,416, floor_plan 1,843, om 791, financials 37; upgrade-only);
`cre_geo_backfill.py --apply` derived 85,618 of 87,328 rows (county 85,618,
cbsa_code/cbsa_name 83,815, geo_source 85,618; crosswalk_zip 77,499, source
4,368, crosswalk_latlng 3,751, 1,710 no hit). Board UNCHANGED at **87,328 active**
(0 non-active, 92,699 total); status was NEVER touched (activation stays OPT-IN
default-off). Consumer views resolve unchanged (`v_cre_active_for_sale` 33,824,
`v_cre_active_for_lease` 58,727, `v_cre_listings_full` 87,328).
At that 2026-06-15 checkpoint, `cre_listing_om_facts`, `cre_listing_media`, and
`cre_listing_links` were empty. Do not treat those child-table counts as current:
a 2026-07-10 read-only contract check observed 398,040 OM-facts rows. 738 pytest
pass (code unchanged that session). STILL GATED for separate go-ahead: live status activation, the consumer
board-gate deploy + widened `005`/`006` views, the media backfill
(`backfill_media_from_raw_data.py`; no longer DDL-blocked now that `011` is
applied), `sql/010` + the enrichment-cadence cutover, and the weekly
mark-missing soft-delete escalation.

**2026-06-14 (data + automation completion).** `colliers-main` full run COMPLETE
and ingested additively: **15,829 active** (5,750 sale + 8,897 lease + 1,182
sale_or_lease), 0 soft-deleted, 0 duplicate external_ids. Live board now
**87,328 active** (0 non-active, 0 NULL status). Phase-2 status activation is now
OPT-IN in `cre_ingest.py` (default OFF; `--activate-status` / `CRE_ACTIVATE_STATUS`),
so routine and scheduled ingests refresh listing data without flipping board
state; the status-display rollout (consumer board-gate, live activation, widened
`005` views) stays deferred. Live DB hardening applied (cap_rate/occupancy range
CHECKs, 4 audit FKs `ON DELETE SET NULL`, 2 unique indexes `NULLS NOT DISTINCT`,
`v_cre_listings_full` `security_invoker`; staged at
`../sql/advisor-reports/2026-06-13-cre-live-hardening.sql`). Data-quality
cleanups: 50 board-invisible JLL rows -> `inactive`, transwestern notes restored,
Savills residential contamination removed (101 sale + 1 ghost lease soft-deleted;
2 defensible Chicago retail lease rows remain). Automation at 2026-06-14: only
`ai.agentic.cre-monitor` (every 3h) and `ai.agentic.cre-daily` (06:30) were
loaded; `cre_gate.py` was wired into `cre_daily_update.sh`. Superseded by the
2026-07-05 documentation snapshot. That snapshot is not current scheduler
evidence; use `bash cre_status.sh` and the 2026-07-11 operator runbook.

Historical snapshot last updated: 2026-07-05. Change-tracking / monitor layer (migration 007 +
`cre_monitor.py` + `cre_gate.py` + `collect.ts --monitor`) built, hardened, and
adversarially reviewed. 2026-06-13 session: `collect.ts` split into cohesive
modules (`types.ts`, `lib/`, `sources/<broker>.ts`; `collect.ts` stays the CLI
entry); monitor exclusions expanded to four (`jll`, `jll-investor`,
`cbre-dealflow`, `colliers`) for detail-derived ids; the coverage gate now
refuses disappearance on errored or truncated passes; 237 tests pass; committed
at `8d38e9cac`. The first gated `cre_monitor.py --apply` seed ran on
`avison-young` (seeded `cre_source_baseline`=1 and `cre_source_index`=2199, zero
events/queue/soft-deletes, board unchanged at 72,544). See
`HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` and
`docs/firecrawl-ops/references/cre-monitor-subsystem.md`.
Prior listing-ingest evidence (2026-06-12 local time), from run finished at `2026-06-12T04:31:24.562Z`, validation on 2026-06-12, CBRE Deal Flow plus Colliers SalesTracker full ingests, NAI active-status-filtered ingest, Cushman full live ingest, Transwestern full live ingest, Marcus & Millichap full public sale ingest, and Lee & Associates full Buildout ingest on 2026-06-12. JLL Investor full sitemap detail ingest finished 2026-06-12 22:47 UTC (934 U.S. sale rows live); 50 stale early-probe rows soft-deleted 2026-06-12 ~23:25 UTC after user approval. Avison Young full detail-enriched ingest finished 2026-06-13 00:35 UTC and was live-ingested additively with 2,201 active rows.

This directory is the production daily path for public commercial real estate listing inventory feeding EQUIRE. Use it for sale and lease listings. The older `../cre_scrapers/` Python package is legacy support for source probes and detail-page enrichment.

## Current State

**Live board and per-source counts:** see the 2026-07-05 banner and Latest Source
Matrix below (107,783 active as of that snapshot). The subsection below is a
historical artifact record from 2026-06-11 through 2026-06-14.

Latest full artifact (historical):

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
  live Supabase active rows total 72,544 as of 2026-06-13 (the +944 over the
  2026-06-12 figure is the colliers-main bounded 943-row batch).
- 2026-06-14: the colliers-main full run was ingested additively (colliers
  brokerage 2,115 -> 17,001 active) and the Savills residential cleanup removed
  102 rows, so the live board reached **87,328 active** (superseded; see banner).

## Next Steps

Canonical go-forward plan: section 14 of
`docs/firecrawl-ops/references/cre-intelligence-system-design.md` (verified
per-source method audit plus the authorized build sequence in 14.4). Two tracks
are open:

1. **DONE 2026-06-14: `colliers-main` full run.** The full ~15,883-URL sitemap
   detail run converged (0 errors) against the resumable JSONL cache
   (`out/cache/colliers-main/`, 15,888 rows) and was ingested additively
   (`--no-mark-missing`, status activation OFF): 15,829 active rows (5,750 sale
   + 8,897 lease + 1,182 sale_or_lease), 0 soft-deleted, 0 duplicate
   external_ids. Colliers brokerage total is now 17,001 active (board 87,328 at
   2026-06-14; superseded). Main-site coverage is complete. See
   `HANDOFF_COLLIERS_MAIN_2026-06-13.md`.
2. **Change-tracking / monitor layer (007, observe-only) - hardened, first
   seed live.** Per section 14.4, complete and adversarially reviewed:
   - Migration 007 applied to prod (`cre_source_index`, `cre_listing_events`,
     `cre_enrichment_queue`, `cre_source_baseline`) plus the 002/004/005 ALTERs
     (widened status CHECK; neutral `source_lastmod`/`canonical_key` columns;
     `v_cre_recent_changes`). Registered in `../sql/000_run_all.sql`.
   - `collect.ts --monitor`: cheap enumeration-only pass. Four sources emit 0
     monitor rows because their persisted `external_id` is detail-derived and
     unrecoverable from cheap enumeration, so they stay on the full-sweep
     cadence: `jll` (numeric `property.id`), `jll-investor` (Salesforce
     `listing.id`), `cbre-dealflow` (`data.projectid` vs URL `listingPv`,
     ~78% mismatch), and `colliers` SalesTracker (SLP `ProjectId` vs
     index-paired map id, ~45% mismatch). `colliers-main` (sitemap `usa#####`)
     stays monitor-enabled. A 0-row monitor run writes an empty artifact instead
     of throwing.
   - `cre_monitor.py` + `cre_gate.py`: observe-only (never write
     `cre_listings.status`/`deleted_at`). Disappearance is triple-gated: the 0.7
     coverage fraction, `run_source_keys` membership, and a refusal for any
     source whose pass errored OR truncated this run (the last is not overridable
     by `--force-disappear`). Re-run `python3 -m pytest tests/ -q` for the
     current count (1402 pass as of 2026-07-05).
   - `collect.ts` is now modular (`types.ts`, `lib/`, `sources/<broker>.ts`);
     `collect.ts` stays the unchanged CLI entry. `npm run typecheck` clean.
   - Full operational rules and gotchas:
     `docs/firecrawl-ops/references/cre-monitor-subsystem.md`.

   DONE 2026-06-13 (gated, verified): the observe-only seed now spans ALL 11
   monitor-enabled sources. `cre_gate.py --apply --update-baseline` then
   `cre_monitor.py --apply` on the all-source `--monitor` artifact
   (`out/monitor/seed_all_2026-06-13.json`, mode=monitor, page-cap 60 to match
   the scheduled tier) seeded `cre_source_baseline`=11 sources and
   `cre_source_index`=73,693 rows, with 0 events / 0 queue and the board
   unchanged (72,544 live active, 0 live non-active, 0 NULL status). The 10 new
   sources were baseline-seeded silently; `avison-young` re-enumerated to the
   same 2,199 keys (0-event diff). `jll`/`jll-investor` now short-circuit their
   monitor pass before enumeration (no wasted paging). See
   `HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` and the Phase-2 board-impact doc.

   Phase-2 status activation is now WIRED and hardened in `cre_ingest.py`
   (Choice (a) COALESCE + terminal-stickiness guard + default-off status-flip
   circuit breaker `CRE_STATUS_FLIP_MAX_FRACTION`). It activates **only** when
   `--activate-status` or `CRE_ACTIVATE_STATUS=1` is set on a daily/manual full
   ingest, NOT from the monitor path and NOT on routine scheduled ingests. The matching
   EQUIRE board-gate widening (Option B) is committed on
   `dynamically-display-cre-listing-data` branch `feat/multi-source-live-listings`
   (not deployed). Gate-0 (prod status CHECK) verified to already allow
   uc/pending/off_market.

   STILL GATED for explicit go-ahead (2026-06-13 snapshot, updated 2026-06-14):
   deploying the T3.2 consumer branch (must deploy BEFORE T3.1 activates),
   triggering the first live T3.1 activation, and applying the widened
   agent-facing `005` views (now `status IN ('active','under_contract','pending')`
   on this branch; live DDL apply gated, verified read-only as a zero-row no-op).
   NOTE: tiered schedules and `cre_gate.py` wiring shipped in code. A 2026-07-05
   document snapshot claimed an enrichment cadence cutover, but the 2026-07-11
   audit found no active CRE scheduler. See the top handoff banner.

The EQUIRE-facing view-gate / status activation (sections 12.4, Phase-2) stays
pending CRE_EQUIRE coordination and is NOT part of the additive build. Board
impact is quantified in
`docs/firecrawl-ops/references/cre-phase2-board-impact-2026-06-13.md`.

## Latest Source Matrix

Historical Supabase snapshot from `credeals` (2026-07-05). **Re-query before
quoting;** see **Agent rule: verify counts** at the top of this file. Board total
**107,783 active** includes ~11,144 rows under additional seeded brokerages
beyond this 20-source collector matrix (regional NAI franchises and similar).

| Source | Active rows (sale / lease / sale_or_lease) | Status |
|---|---:|---|
| CBRE | 19,028 (4,222 / 13,144 / 1,662) | Internal JSON API through local Firecrawl stealth |
| CBRE Deal Flow | 1,836 (1,809 sale + 27 lease) | Public RCM ListingEngine; folded into `cbre` with `dealflow:` prefix |
| JLL | 10,741 (1,247 / 8,733 / 761) | Main public property feed with detail enrichment |
| JLL Investor | 934 sale | Folded into `jll` with `investor:` prefix; full sitemap detail run |
| Cushman & Wakefield | 11,318 (2,743 / 8,575) | Public API feed with detail enrichment |
| Newmark | 4,374 (1,123 / 3,249 / 2) | Public Algolia feed with People contacts |
| Marcus & Millichap | 3,124 sale | Public map ActivityIds + detail HTML; lease unsupported |
| Avison Young | 2,201 (636 / 1,432 / 133) | SharpLaunch feed with detail enrichment |
| Savills | 2 lease | Sale STRUCTURALLY CAPPED; 2 Chicago retail lease rows |
| SVN | 5,287 (2,660 / 2,189 / 438) | Public Buildout feed with durable page cache |
| NAI Global | 241 (183 / 58) | Infabode GraphQL; `FOR_SALE_ON_MARKET` filter only |
| Lee & Associates | 9,223 (2,611 / 5,691 / 921) | Public Buildout feed with durable page cache |
| Colliers (SalesTracker) | 1,172 sale | RCM investment-sale subset; folded into `colliers` |
| Colliers main (`colliers-main`) | 15,829 (5,750 / 8,897 / 1,182) | XML sitemap + JSON-LD detail; `main:` prefix |
| Transwestern | 2,021 (389 / 1,502 / 130) | Public GET feed with detail enrichment |
| Matthews | 3,563 (2,912 / 651) | Public sitemap + direct fetch (added 2026-06+) |
| Franklin Street | 413 (218 / 186 / 9) | Buildout inventory (`franklin-street`); sale/lease tokens |
| SRS | 2,122 (1,111 / 844 / 167) | SRS backend search API, paginated POST |
| Hanley | 102 sale | Embedded `rethink_properties` JSON on `/listings/` |
| Kidder Mathews | 3,108 (823 / 2,258 / 27) | Kidder backend search API, paginated POST |

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
9. `docs/firecrawl-ops/references/cre-monitor-subsystem.md` (monitor/change-tracking layer: components, run model, hard gotchas) when touching 007, `--monitor`, `cre_monitor.py`, or `cre_gate.py`
10. `HANDOFF_COLLIERS_MAIN_2026-06-13.md` (colliers-main full run, COMPLETE 2026-06-14: converged + ingested additively, 15,829 active main-site rows)
11. `HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` (monitor hardening + collect.ts modular refactor + first gated `--apply` seed) when touching the monitor layer or scaling the seed
12. `ENRICHMENT_WORKER_DESIGN_2026-06-15.md` (Tier-B `cre_enrich.py` queue worker + cadence restructure: monitor 2x/day, enrich every 4h, weekly additive backstop, daily retired; IMPLEMENTED in code, live cutover gated) when touching `cre_enrich.py`, `collect.ts --enrich-input`/`lib/enrich.ts`, `sql/010`, or the launchd tier set

Historical buildout/validation detail (handoff log, lessons, validation
snapshots, egress and security audits) lives in `archive/`; see
`archive/README.md` for the index and the durable nuggets each file still holds.

**Fresh machine (new clone, Mac mini, or this MacBook Pro)?** Start with
`SETUP.md` and run `bash cre_setup.sh` (one-command preflight + bootstrap:
toolchain, deps, env, stack, offline smoke). Then continue below.

Then run:

```bash
cd <repo>                         # the firecrawl clone root on this machine
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
bash cre_setup.sh --check        # read-only health snapshot (skip if you just ran full setup)
bash cre_status.sh               # run-health heartbeat (schedules, last runs, staleness)
npm run typecheck
python3 -m py_compile cre_ingest.py
npm run validate:supabase -- --out /tmp/cre_validate_latest.md
```

## Safe Daily Command

Use this while any all-source errors or partial source decisions remain:

```bash
cd <repo>/scripts/firecrawl-ops/cre_collector   # the firecrawl clone root on this machine
bash cre_daily_update.sh --no-mark-missing
```

Use default `bash cre_daily_update.sh` only after a clean all-source run has no Lee/source errors and the per-broker mark-missing guards are acceptable for that day.

## Supabase Access Model

Target project: `fhqycqubkkrdgzswccwd`, schema `credeals`.

The ingestor reads `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from the env
file discovered by `load_db_url()` (`--env-file`, then `CRE_ENV_FILE`, then
`~/Documents/...` defaults). On this Mac, launchd sets
`CRE_ENV_FILE=~/.config/cre/equire.env`. It shells out to `psql` and prints only
the env file path, never the credential value.

The collector-owned `cre_*` base tables and `v_cre_*` views are service-role only. `anon` and `authenticated` do not have table or view `SELECT`. RLS is enabled with no public row policies by design. The display views use `security_invoker=true`, and `search_cre_listings(...)` plus `update_cre_listing_timestamp()` are executable by `service_role`, not by public browser roles.

If the UI-side live-board plan docs disagree with this posture, prefer
`archive/CONTRACT_SYNC_2026-06-12.md` plus the later UI-side hardening SQL notes.

Document and image tables store source URLs only. Do not download public PDFs or
images into Supabase storage for the bulk collector.

## Known Limits To Respect

- **Scheduler audit (2026-07-11).** No CRE launchd tier, marker, or collector
  artifact exists on the Mac mini. The 2026-07-05 document snapshot claiming
  loaded tiers and failures is historical only. Fresh-machine setup remains in
  `SETUP.md`, but runtime recovery is gated by the operator runbook.
- **Enrichment implementation.** `cre_enrich.py`, `collect.ts --enrich-input`,
  `sql/010` health views, and plist templates are implemented in code. They are
  not currently loaded on the Mac mini. See the operator runbook, not the
  historical Section 9 design, for recovery authority.
- Do not use `--mark-missing` after a run with Lee or other source errors.
- Cushman & Wakefield is now current in Supabase from `out/cushman_full_2026-06-12_022841.json`: 11,318 active rows, 18,343 document URL rows, 24,278 image URL rows, 21,110 contact rows, 21,110 profile URLs, and 20,301 VCard URLs. Source-scoped `--mark-missing` soft-deleted 24 old probe rows.
- CBRE Deal Flow has been ingested from the public RCM endpoint. Do not use its reported 2,042 sale total as collected count; the public card pagination exposed 1,809 sale cards in the full run. A narrow cleanup soft-deleted 21 stale `dealflow:url:<sha1>` rows that duplicated newer enriched Deal Flow IDs.
- Do not store source PDF or image binaries in Supabase. Store URLs only.
- Colliers now has two folded sources under the `colliers` brokerage. SalesTracker (`colliers`, 1,172 investment-sale rows) via public RCM GET. Main site (`colliers-main`, COMPLETE 2026-06-14: 15,829 active rows) via the public XML sitemap (`/sitemap` -> `en/sitemap?type=properties`, ~15,883 detail URLs) fetched through local Firecrawl plus detail-render JSON-LD parse; ids prefixed `main:`. The Coveo POST search is still not used and not needed. Full run converged via `run_colliers_main_full.sh` (chunked, resumable cache) and was ingested additively (status OFF); colliers brokerage total 17,001 active.
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
- Savills sale is STRUCTURALLY CAPPED, not completable: the public U.S. sale
  surface serves Savills Residential luxury homes only (no public commercial-sale
  JSON / `__NEXT_DATA__` feed). On 2026-06-14 the 101 mis-categorized residential
  "sale" rows and 1 non-U.S. ghost lease row (`cyelit10899`) were soft-deleted,
  leaving 2 defensible Chicago retail lease rows. Treat current coverage as the
  permanent Savills baseline. The commercial-sale route
  `/com/en/list/commercial/property-for-sale/united-states-of-america` WAS probed
  (22-URL test matrix, all returned HTTP 200 with `totalItems:0` or non-US
  Canada/UK/Ireland objects). The cap is confirmed: no public US commercial-sale
  feed exists on Savills. See `FRESHNESS_HISTORY_REVIEW_2026-06-15.md` section R1.
- `cre_ingest.py` now drops non-HTTP contact profile/avatar/VCard URLs and
  non-HTTP document URLs. Reingesting the complete Lee and SVN artifacts
  refreshed child rows and reduced active bad contact avatar URLs from 37 to 0.
- `cre_ingest.py --mark-missing` now refuses incomplete folded source coverage.
  For parent slugs with sub-sources, such as `cbre` plus `cbre-dealflow` or
  `jll` plus `jll-investor`, all known source keys must be present in the same
  ingest batch before parent-level soft deletes can activate.
- **Additive migrations (2026-06-15) are APPLIED to prod; history is live.**
  Migration `009_cre_history_retention.sql` (APPLIED 2026-06-15, verified) added
  `cre_listing_price_history`, `cre_listing_contacts_archive`,
  `cre_listing_documents_archive`, three `prior_*` columns on `cre_source_index`,
  and the `trg_cre_listings_block_history_delete` retention trigger; the ingestor
  writes price-history snapshots on watched-field changes (existence guards keep
  it safe regardless). Migrations `011`/`012`/`013`/`014` are ALSO applied to
  prod (2026-06-15): `011` media/links tables + widened `doc_type` CHECK, `012`
  institutional + geo columns, `013` `cre_listing_om_facts`, `014`
  `cre_zip_cbsa_crosswalk` (33,791 rows). The three additive backfills
  (`cre_backfill_raw_data.py`, `om_classify_existing.py`, `cre_geo_backfill.py`)
  also ran `--apply`, all COALESCE-keep, board unchanged at 87,328 active. See
  the 2026-06-15 banner at the top of this file.
- **Weekly mark-missing, status-activation go-live, and the consumer board-gate
  deploy remain GATED for explicit go-ahead.** Do not pass `--activate-status`,
  enable the `CRE_WEEKLY_MARK_MISSING=1` soft-delete escalation on the weekly
  tier, or apply the widened `005` views until coordinated with the EQUIRE
  CRE_EQUIRE deploy. See the phase2 board-impact doc's activation runbook. The
  weekly tier itself is now ADDITIVE by default (`--no-mark-missing`), so loading
  it as the backstop is safe; only the soft-delete escalation is held.
- **Enrichment scheduler status.** The 2026-07-05 failures are historical
  observations, not a currently running tier. Use the read-only preflight and
  operator runbook before any recovery. Weekly mark-missing escalation
  (`CRE_WEEKLY_MARK_MISSING=1`) remains gated.
- Do not treat legacy `cre_scrapers` active flags as production collector status.
- Do not stage `node_modules/`, `out/`, `__pycache__/`, or generated SQL artifacts.

## Operational Recovery

**Dormant until explicit runtime-recovery and scheduler approval.** The Mac
mini currently has no CRE scheduler installed. Start with `bash cre_status.sh`
as a read-only preflight, then follow the operator runbook. Do not use the
historical launchd recovery commands below to create or restart a job.

- **Missed or failed scheduled run.** Do not re-kick a tier or run a manual
  catch-up while the scheduler is disabled. Record the read-only preflight and
  obtain the runbook's named approval before any runtime action.
- **Clear a wedged lock.** The tiers serialize on the portable `mkdir` lock dir
  `out/daily/.cre.lock` (plus a transient `out/daily/.cre.lock.reclaim` during
  stale reclaim). `cre_run_tier.sh` auto-reclaims a lock whose recorded PID is
  dead, so a wedged lock means the owner is still alive or `cre_status.sh`
  flagged it "possible hung run". After confirming no live process and receiving
  runtime-recovery approval, quarantine the lock under a timestamped name rather
  than deleting it. Never modify a lock while a real run is active; it exists to
  keep additive and mark-missing work from overlapping.
- **Reclaim disk.** Both runners self-prune on exit (daily keeps 14
  `run_*.json` / 29 `run_*.log` / 14 `gate_*.json` under `out/daily/`; the tier
  dispatcher keeps 24 `monitor_*.json` + 24 `monitor_*.log` under `out/monitor/`
  and caps the launchd `cre-*.{out,err}.log` files), so growth is bounded
  without intervention. `cre_status.sh` warns past ~4GB (`out/daily`) / ~8GB
  (`out/monitor`). If a crash left orphaned artifacts, they are safe to delete
  manually (`out/` is gitignored); the durable source caches under
  `out/cache/` are the only artifacts worth preserving for resumable runs.
