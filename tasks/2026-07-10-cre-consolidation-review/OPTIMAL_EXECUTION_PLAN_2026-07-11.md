# CRE Data Platform: Optimal Execution Plan (2026-07-11)

> **Superseded planning snapshot.** Use it for rationale, not live state. The
> later Mac mini audit found no active CRE scheduler or healthy local runtime;
> the [operator runbook](2026-07-11-firecrawl-operator-runbook.md)
> defines the current gated sequence.

**Status:** Planning-only synthesis. No production DDL, database write, launchd
change, scheduler activation, repository creation, deletion, PR, or merge is
authorized by this document.

**Relationship to prior work:** This file refines `FINAL_PLAN.md` using fresh
runtime evidence, the pushed safety branches, direct inspection of GetCREdata,
aa-hub, and CRE_EQUIRE, and two additional independent planning reviews. Where
this file conflicts with `FINAL_PLAN.md`, this file is the recommended sequence.

## 1. Executive decision

The optimal end state has four code surfaces and one shared data plane:

| Surface | Role | Owns | Must not own |
| --- | --- | --- | --- |
| Firecrawl fork | Generic self-hosted scrape and parse infrastructure | Docker/OrbStack stack, Firecrawl API, reusable scrape/parse tooling | CRE collectors, CRE schema, CRE schedules, EQUIRE product views |
| `cre-listings` (proposed) | Listing acquisition and listing lifecycle | Broker adapters, normalization, monitor/enrich queue, listing tables and archives, raw listing views, ZIP/CBSA crosswalk | Market-data pipeline, product-facing cross-domain RPCs |
| GetCREdata | Market and document intelligence | Federal/public market pipeline, CMBS/REIT/CBRE market tables, OM extraction execution, parser-selection logic, market aggregates | Listing lifecycle, EQUIRE access controls |
| CRE_EQUIRE | Product integration | Listing board/feed RPCs, listing-to-market joins, consumer indexes, grants, and RLS | Broker collection and market-source ingestion |
| Supabase `credeals` | Shared data plane | Tables and views governed object by object | Compute-heavy scraping or one-repo global ownership |

Keep the repositories separate. Consolidate contracts, object ownership,
scheduling, and observability. Do not merge the listing collector into
GetCREdata.

GitHub Actions is ruled out as a scheduler and remains manual-only. The live
listing collector stays on the Mac mini because it depends on the local
Firecrawl endpoint and the current network posture. GitHub-hosted runners
cannot use the Mac's `localhost:3002` Docker stack. aa-hub is historical source
and runbooks, not an execution control plane. Any unattended GetCREdata or
collector schedule requires Cayman's later approval of one named,
policy-compatible coordinator and owner.

## 2. Superseded evidence snapshot

### 2.1 Listing runtime

- This prior synthesis included claims about a healthy local runtime, current
  monitor state, loaded tiers, and an active four-column writer mismatch.
- The later audit supersedes those claims: the Mac mini has no active CRE
  scheduler or healthy local runtime; the Firecrawl writer now uses the
  five-column contract and rejects OM-facts payloads at its ingestion boundary.

Use the operator runbook and execution-status audit for current blockers.

### 2.2 Already-pushed implementation

- Firecrawl branch `origin/fix/cre-consolidation-safety` contains seven commits
  covering failure counters, optional failure webhook plumbing, the five-column
  DDL source, guarded legacy migration, advisory validation, geo state guard,
  source-registry parity, launchd drift reporting, and credential fallback
  warnings.
- GetCREdata branch `origin/fix/cre-market-index-parser-version` at `4f116df`
  selects one parser version per listing before building market aggregates and
  corrects the documented five-column key.
- Production already has the desired five-column unique index and 398,040 OM
  fact rows. Migration `015` is therefore a legacy-alignment migration, not a
  production repair required on the current database.
- Neither feature branch has an authorized PR. The live Firecrawl checkout is
  not yet running the safety branch.

### 2.3 GetCREdata scheduler and unattended-run risks

- The GitHub workflow is manual-only. Scheduled execution has been removed.
- Historical run `28784951619` exceeded the one-hour GitHub timeout while still
  in the HMDA portion of step 14. The current aa-hub stub copies the same
  3,600-second timeout, so that timeout is not evidence-based.
- The newest historical GitHub schedule failed before starting because of the
  account spending state. This reinforces the decision not to use Actions.
- The aa-hub manifest is disabled, exits 78 intentionally, uses the `default`
  environment profile, and has no runnable wrapper.
- `run.py` can print "Supabase export skipped" and still exit successfully when
  credentials are missing.
- Data-validation warnings currently do not block export.
- CMBS batch upserts tolerate partial failures, while the local high-water file
  advances afterward. That can convert a partial write into a false checkpoint.
- The current Mac is a MacBook, not the execution hub. The manifest path must be
  verified on the actual Mac mini before calling it right or wrong.

### 2.4 Schema governance has three code owners, not two

CRE_EQUIRE already owns and migrates product-facing listing RPCs, geo ranking,
OM board indexes, grants, and RLS. Therefore, "one migration home for all of
`credeals`" is the wrong abstraction. The correct rule is one migration owner
per object, recorded in one canonical ownership manifest.

## 3. Non-negotiable operating rules

1. No production DDL without a captured before state, explicit approval,
   transactional apply path, validation queries, and rollback SQL.
2. No production collection canary until the exact code under test is deployed
   to the live checkout.
3. No duplicate scheduled scrapers and no duplicate production writers.
4. No scheduled GetCREdata `--force` runs. Preserve the local cache.
5. No listing status activation or mark-missing behavior in a bounded enrich
   canary.
6. No extraction of a failing pipeline.
7. No GitHub Actions scheduler or required CI workflow.
8. No reliance on a same-host webhook as the only dead-man signal.
9. No deletion without a timestamped backup and verified rollback copy.
10. No repo creation or scheduler activation until Cayman approves that gate.

## 4. Execution sequence

### Wave 0: Contain the current enrichment loop

**Purpose:** Stop repeated paid or anti-bot-sensitive work from ending in the
same known database failure.

With explicit scheduler approval:

1. Temporarily unload enrich and weekly.
2. Permanently unload the retired daily tier, while retaining its plist as a
   rollback artifact.
3. Keep monitor loaded because it is current, observe-only, and supplies the
   source-index freshness signal.
4. Confirm no shared CRE lock is held and monitor produces a new `ok:true`
   marker.

**Exit gate:** monitor is the only active write-adjacent CRE schedule, its marker
is fresh, and no new enrichment batch is claimed before the writer repair.

### Wave 1: Finish and deploy the listing-contract repair

**Purpose:** Make the existing five-column production contract true in every
writer and source file.

1. Amend `fix/cre-consolidation-safety` so the OM child upsert in
   `cre_ingest.py` includes `parser_version` in its conflict target.
2. Add an exact generated-SQL regression test for the five-column target.
3. Add a local ephemeral-Postgres contract test that applies the collector
   migrations and executes the OM upsert against the real constraint. This
   replaces confidence based only on SQL-string assertions.
4. Keep `015` as an idempotent migration for older installations. Do not apply
   it to current production because production is already aligned.
5. Run the complete local verification ladder:

   - Python suite, expected current baseline: 1,392 passed and 17 skipped.
   - TypeScript typecheck and unit suite, expected current baseline: 479 passed.
   - Shell syntax and focused status/daily/ingest tests.
   - `git diff --check` and contract-document review.

6. Reconcile the branch with current `origin/main` without overwriting the
   user's dirty main checkout. Use a clean branch or worktree for integration.
7. Obtain PR authorization before opening a PR. No direct main push.
8. After review and merge approval, update the actual live checkout, rerender
   installed plists, inspect their paths and environment, and only then resume
   canary execution.

**Bounded production canary:**

1. Capture queue counts, stale-claim count, dead-letter count, relevant listing
   snapshots, and the current validation report.
2. Run enrich with `CRE_ENRICH_BATCH=5` through the normal tier wrapper.
3. Require exit 0, an `ok:true` marker, five completed claims, zero released
   claims, no constraint error, no status changes, no soft deletes, and clean
   validation.
4. Resume enrich and require three consecutive scheduled 200-row cycles to
   exit 0.
5. Require two monitor cycles to remain green.
6. Resume weekly in additive mode and require one successful weekly cycle.

**Observation window:** At least seven days, because the weekly proof matters.

**Rollback:** Unload enrich and weekly, revert the live code to the prior
checkout, preserve the queue for retry, and keep monitor running. Do not reload
daily merely because one enrich run fails.

### Wave 2: Establish object-level schema governance

**Purpose:** Prevent the same cross-repo mismatch from recurring.

Create one canonical, machine-readable ownership manifest in the company
EQUIRE context, with pointers from all three repositories. Assign exactly one
migration owner to each object.

Recommended ownership:

| Object family | Migration owner | Writers | Consumers |
| --- | --- | --- | --- |
| `cre_listings`, child/archive tables, source index, enrichment queue | `cre-listings` | `cre-listings` | GetCREdata, CRE_EQUIRE |
| `cre_listing_om_facts` and archive lifecycle | `cre-listings` because of listing FK and retirement lifecycle | GetCREdata only | `cre-listings`, CRE_EQUIRE |
| OM parser-release and current-document selection views | GetCREdata | GetCREdata | GetCREdata, CRE_EQUIRE |
| ZIP/CBSA crosswalk | `cre-listings` | deterministic Census rebuild/load process | GetCREdata, CRE_EQUIRE |
| CMBS, REIT, CBRE survey, market tables and market aggregates | GetCREdata | GetCREdata | CRE_EQUIRE |
| Listing board/feed RPCs, listing-to-market joins, consumer indexes, grants, RLS | CRE_EQUIRE | CRE_EQUIRE migrations | EQUIRE app and agents |

Required contract gates:

1. Firecrawl or `cre-listings` test: table constraints and every conflict target
   match exactly.
2. GetCREdata test: external-writer conflict target and parser-selection view
   match the manifest.
3. CRE_EQUIRE test: RPC signatures, return types, dependent columns, grants,
   RLS, and indexes match producer contracts.
4. Live-ledger reconciliation: compare planned DDL to `pg_get_viewdef`,
   `pg_indexes`, `information_schema`, and the Supabase migration ledger before
   apply.
5. Status docs are generated from read-only checks, not manually maintained
   counts or tier labels.

**Important semantic decision:** GetCREdata is the only OM extraction writer.
The collector's `om_parse.py` stays retired. The listing domain retains the
base table and archive lifecycle because OM rows are children of listings.

### Wave 3: Harden GetCREdata for unattended execution

**Purpose:** Make a successful process exit mean the production market data is
complete enough to trust.

Complete these changes on a GetCREdata feature branch before any unattended
coordinator activation:

1. Add a self-locating scheduled wrapper that uses a process lock, the repo
   virtual environment, persistent cache, structured run summary, and explicit
   exit propagation.
2. Add preflight and validation-only modes that do not write production data.
3. Require the seven production secrets through a dedicated `getcredata`
   aa-hub environment profile. Missing Supabase export credentials must fail.
4. Make partial Supabase exports fail the scheduled run.
5. Convert validation from informational output into explicit thresholds for
   unattended runs. Warnings may remain advisory, but critical coverage,
   freshness, row-count, and required-source failures must block export or fail
   the run.
6. Advance the CMBS high-water file only after all required batches write
   successfully and the post-write verification passes.
7. Classify pipeline steps as required, degradable, or optional. A required
   CMBS/REIT/market-export failure must not be printed as a harmless skip.
8. Add focused tests for missing secrets, partial exports, validation failure,
   lock contention, timeout handling, required-step failure, and high-water
   suppression.
9. Preserve the already-pushed parser-version view fix. Before applying that
   view, decide whether multiple documents within the selected parser version
   should be combined or whether one latest document must also be selected.

**GetCREdata view DDL gate:**

1. Capture the current `cre_market_index` definition, dependent views, grants,
   row count, and selected metric summaries.
2. Run the proposed definition as a preview query and diff its results without
   replacing the production view.
3. Apply in a transaction with lock and statement timeouts only after review.
4. Require unchanged public schema, successful dependent queries, preserved
   grants, expected row population, and explained metric deltas.
5. Roll back by restoring the captured definitions in a transaction.

### Wave 4: Activate GetCREdata on an approved coordinator

**Purpose:** Restore automated market-data freshness without GitHub Actions.

1. On the actual Mac mini, verify the intended checkout path, branch, Python
   runtime, free disk, cache path, and access to required endpoints.
2. Record Cayman's approval of the named coordinator, responsible owner,
   credential boundary, exact rendered job, observation window, and rollback.
3. Run from a clean, current GetCREdata checkout and pinned virtual environment.
4. Keep the routine schedule on `python run.py`, never `--force` or `--excel`.
5. Use a dedicated `getcredata` env profile and keep secrets out of git.
6. Schedule after the 21:30 listing enrichment window, provisionally 22:15
   America/Chicago. This provides roughly three hours before the 01:30 enrich
   window and should finish before morning product use.
7. Set the first controlled-run timeout to 10,800 seconds, not 3,600. The
   one-hour historical timeout already failed. After two measured warm-cache
   runs, reduce the timeout to observed p95 plus a documented margin.
8. If a warm-cache run still exceeds 90 minutes, split high-latency sources by
   source cadence rather than increasing one monolithic timeout indefinitely.
   HMDA and full BPS refreshes are the first candidates.
9. Before scheduling, confirm PITR or restorable snapshots for the tables the
   pipeline mutates.
10. Run one supervised production proof without `--force`:

   - Process exits 0.
   - No required fetch or export batch fails.
   - Critical validation gates pass.
   - Expected CBSA and market tables remain within reviewed baseline bands.
   - Freshness metadata advances.
   - Before/after samples and aggregate deltas are explainable.
   - Runtime and per-step durations are saved.

11. Update the company automation registry for the approved coordinator. Do
    not create a scheduled GitHub Actions workflow or treat aa-hub as runtime
    authorization.
12. Require `bin/ci-local`, `bin/aa-doctor`, and
   `bin/render-launchd.sh --dry-run` to pass.
13. Require founder review before `render-launchd.sh --apply`.
13. Confirm the first run in `runs.jsonl`, `ops.job_runs`, expected cadence,
    alarms, and alert delivery.
14. Observe seven consecutive days before declaring the lane healthy.

The GitHub workflow remains manual disaster recovery only. There is no
scheduled twin to run in parallel.

**Rollback:** Unload `ai.aa.getcredata-pipeline`, set the manifest disabled,
remove its allowlist entry, rerender, and use supervised manual hub runs until
the defect is corrected.

### Wave 5: Complete observability and freshness semantics

**Purpose:** Detect both process failure and host failure, and make listing
freshness useful to EQUIRE.

1. Configure and test the already-implemented collector failure webhook as an
   immediate same-host signal. Force one safe preflight failure and require one
   alert with the original exit code preserved.
2. Add a read-only CRE health job on the explicitly approved coordinator that
   checks tier markers, queue age,
   last successful ingest, validation output, and local Firecrawl health.
3. Use Supabase `ops.evaluate_alarms` and `ops.page_alarms` as the off-host
   dead-man. A coordinator cannot detect its own powered-off host.
4. Alarm when monitor/source-index freshness exceeds the source-specific SLO,
   when GetCREdata exceeds its expected cadence, or when queue age and dead rows
   breach thresholds.
5. Generate a dated status artifact from markers, database probes, and approved
   coordinator run records. Replace hand-maintained CLAUDE status banners with
   pointers.
6. Expose listing freshness through a view joining `cre_listings` to
   `cre_source_index.last_enumerated_at`. Do not update
   `cre_listings.last_seen_at` on every monitor pass, because that would churn
   `updated_at` and duplicate the source index.
7. Run the existing read-only data-quality audit on a reviewed cadence and
   retain its JSON and Markdown summaries outside the repo working tree.

### Wave 6: Ship product unlocks in CRE_EQUIRE

**Purpose:** Convert the stabilized data contract into user value.

CRE_EQUIRE, not the collector or GetCREdata, should own:

1. `v_cre_listing_market_context`, joining listing `cbsa_code` to governed
   market metrics.
2. A CBSA-grouped market summary that replaces raw city/state fragmentation
   without breaking the existing RPC contract.
3. `get_listing_with_market_context(listing_id)` or the equivalent governed
   RPC/MCP call.
4. Consumer indexes, grants, RLS, and query-plan verification.
5. Per-listing freshness fields derived from source-index timestamps.

Before production apply:

- Verify null and mismatched CBSA rates.
- Compare listing counts before and after the join.
- Require left-join semantics so absent market data never hides a listing.
- Validate RLS and service-role behavior.
- Smoke-test the board, feed, market summary, and MCP paths.
- Capture and compare query plans and latency.

Follow with observe-only cross-broker duplicate grouping. Flag probable matches
using normalized address, ZIP, and size tolerance. Do not auto-merge records.

### Wave 7: Extract `cre-listings` after both systems are stable

**Approval gate:** Creating the repository requires explicit Cayman approval.

Preconditions:

- Listing repair completed, including one successful weekly cycle.
- GetCREdata completed its seven-day canary on the explicitly approved
  coordinator.
- The three-owner manifest is adopted.
- No unresolved production data-integrity incident remains.

Extraction scope:

1. Preserve history for the active collector, CRE SQL, relevant tests,
   fixtures, and durable operator docs.
2. Do not move the Firecrawl Docker stack, generic Firecrawl CLI/MCP tools,
   legacy `cre_scrapers`, or unrelated `scripts/firecrawl-ops` assets.
3. Replace `FC_DIR` and Docker-compose assumptions with a small endpoint health
   contract based on `FIRECRAWL_API_URL`.
4. Provide one local verification command covering Python, TypeScript,
   shell, schema-contract, and fixture checks. This is the required evidence
   command in place of GitHub Actions.

Dark verification must not double-run production work:

1. Replay one captured collector artifact through both old and new dry-run
   ingest paths and compare normalized generated SQL.
2. Run one bounded, write-disabled live collect and compare artifacts, source
   counts, error classifications, and parser output.
3. Do not schedule both copies against broker sites.
4. Do not allow both copies to write production.

Cutover:

1. Author jobs for the explicitly approved coordinator for monitor, enrich,
   and weekly. Do not recreate the retired daily tier.
2. Render and review all paths, environment profiles, schedules, timeouts, and
   shared-lock location.
3. Pause the legacy launchd jobs, apply the approved coordinator jobs
   atomically, and
   verify one run of each lane. Parallel live twins are prohibited because the
   jobs share sources and write surfaces.
4. On failure, unload the approved coordinator jobs and reload the preserved
   legacy plists.
5. After seven green days, remove the legacy loaded jobs while retaining the
   rollback artifacts for 30 days.

### Wave 8: Clean the Firecrawl fork and finish resilience work

Only after the extraction rollback window:

1. Write timestamped backups of every file or tree to be removed.
2. Remove the active CRE collector and CRE schema from the Firecrawl fork,
   leaving a clear pointer to `cre-listings`.
3. Remove the unscheduled legacy `cre_scrapers`, `cre_pipeline.py`, and tracked
   reference bloat after confirming they are absent from runtime and tests.
4. Shrink the upstream-sync protected-path list.
5. Verify a normal upstream sync no longer touches CRE domain code.
6. Verify Supabase PITR and perform a restore drill into an isolated target.
7. Add per-run scrape and model cost accounting before introducing soft spend
   caps.
8. Add source-specific fetch policy and residential-proxy configuration only
   where measured broker failures justify it.
9. Prototype vision fallback only for sources with a proven structural cap.

## 5. Improvement-idea disposition

### Execute now or in the first two waves

- Live truth checks and tier root cause.
- Five-column OM contract across DDL and both writers.
- Consecutive failure counters and alert configuration.
- Credential and installed-plist drift warnings.
- Advisory validation and geo state guard.
- Source-registry parity.
- GetCREdata hardening and approved coordinator scheduling.
- Retired daily-tier removal.

### Execute after stabilization

- Off-host dead-man through Supabase alarms.
- Backup and restore drill.
- Ephemeral-Postgres ingest test.
- Three-owner schema contract.
- Canonical crosswalk version and refresh cadence.
- Listing-market join, CBSA market summary, freshness view, governed MCP/RPC.
- Observe-only cross-broker duplicate grouping.
- Generated status artifacts.

### Defer until evidence justifies them

- Per-run price/status provenance tags.
- Cloud collector containerization.
- Split tier locks.
- Additional queue schema work.
- LLM vision fallback.
- Soft spend caps.

The current shared lock is a safety feature. With daily retired and enrich
normally completing in minutes, do not split it until measured lock contention
causes missed freshness SLOs.

The current queue already has retry, claim release, attempts, and dead-letter
behavior. Improve observability before adding another queue redesign.

### Reject

- Full codebase merge.
- GetCREdata merged into `cre-listings`.
- GitHub Actions scheduler or required CI.
- One repository owning every `credeals` migration.
- Two OM extraction writers.
- Parallel live dark runs.
- Routine daily full brokerage collection.
- Writing `cre_listings.last_seen_at` on every monitor pass.
- Running the browser-heavy collector on Supabase, Vercel, or GitHub-hosted
  compute before source-specific network tests prove it viable.

## 6. Timeline and decision gates

| Period | Outcome | Approval needed |
| --- | --- | --- |
| Day 0 | Pause failing enrich/weekly and retire daily | Scheduler mutation approval |
| Day 1 | Writer repair locally green and reviewable | PR authorization if a PR is desired |
| Days 1-7 | Bounded enrich, scheduled enrich/monitor, and weekly canary | Production canary approval |
| Days 2-5 | GetCREdata unattended-run hardening | GetCREdata code approval only |
| Days 5-12 | Supervised GetCREdata proof and seven-day coordinator canary | Production run and named coordinator activation approval |
| Days 5-10 | Three-owner manifest and contract tests | Cross-repo owner acknowledgement |
| After both canaries | CRE_EQUIRE product views and RPC | Production DDL approval |
| After both canaries | Create and extract `cre-listings` | New-repository approval |
| Seven days after extraction | Remove legacy loaded schedules | Scheduler mutation approval |
| Thirty days after extraction | Fork cleanup and rollback-artifact retirement | Deletion approval and backups |

## 7. Definition of done

The consolidation program is complete only when all of the following are true:

1. Monitor, enrich, and weekly have current green evidence; daily is unloaded.
2. Every OM writer and constraint uses the five-column contract.
3. GetCREdata runs through the explicitly approved coordinator for seven days
   with correct exports,
   validation, cadence, and off-host alarms.
4. Each shared object has one named migration owner and contract tests protect
   cross-repo consumers.
5. CRE_EQUIRE owns and verifies listing-market product integration.
6. The collector runs from `cre-listings` with no duplicate scheduler or writer.
7. The Firecrawl fork contains scraping infrastructure only.
8. Backup, rollback, and restore evidence exists.
9. All implementation branches are clean, committed, pushed, and handed off
   through authorized review paths.

## 8. Immediate next action when execution resumes

Do not start with repo extraction or scheduler activation. Start by completing
and merging the reviewed repository repairs, then recover the runtime only
after explicit approval. The bounded five-item additive canary and any
unattended coordinator activation remain separate later approvals.
