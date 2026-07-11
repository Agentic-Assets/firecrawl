# OM-facts writer repair closeout (2026-07-11)

**Branch:** `fix/cre-consolidation-safety`  
**Base:** `origin/main` at `c74ece496`  
**Implementation commits:** `477331e70` (`fix: align OM facts upsert contract`)
through `2dc47f078` (`fix: retire Firecrawl OM writer`)
**State:** pushed to `origin/fix/cre-consolidation-safety`; draft
[PR #22](https://github.com/Agentic-Assets/firecrawl/pull/22) targets `main`.
No production DDL, launchd change, collector run, or production database write
was performed.

## Goal

Repair the confirmed listing-writer mismatch: production already has a
five-column `cre_listing_om_facts` unique key, while the collector emitted a
four-column conflict target and failed every OM-facts ingest.

## What shipped

- `scripts/firecrawl-ops/cre_collector/cre_ingest.py` now emits
  `ON CONFLICT (listing_id, fact_group, fact_key, source_doc_url,
  parser_version) DO UPDATE`. A repeat parse from the same parser release
  updates its row; a different parser release remains separately auditable.
- `scripts/firecrawl-ops/cre_collector/tests/test_cre_ingest_history.py` now
  asserts the exact generated conflict target, rejects the former target, and
  checks that the source migration declares the same five-column identity.
- `PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md` and
  `HANDOFF_DATA_LIFT_2026-06-15.md` now describe the canonical five-column key,
  removing the stale four-column contract from collector documentation.
- `cre_enrich.py` no longer exposes `--om-parse`, reads `CRE_OM_PARSE`, or
  invokes `om_parse.py`. A legacy launchd environment cannot reactivate a
  second OM writer.
- `om_parse.py --apply` now exits `78` before any database, parse, or ingest
  work. GetCREdata is the sole production OM extraction writer; the pure
  extractors and dry-run artifact path remain available for regression coverage.
- `tests/run_om_facts_postgres_contract.sh` is a reproducible, opt-in
  PostgreSQL 17 contract runner. It applies source migration `013`, executes
  the generated production upsert three times, proves a same-version update and
  a cross-version coexistence, and removes its unexposed container on exit.
- `CREDEALS_OWNERSHIP.md` now assigns `cre_market_index` to GetCREdata and its
  reviewed migration/export path. Firecrawl is read-only for that object.
- Root agent guidance now links the proposed ownership manifest's review branch
  and explicitly prohibits resolving it from `$AA_CONTEXT_ROOT` before
  AGENTIC-1233 merges it into Context Engineering.

## Verification

- Focused Python contracts:
  `python3 -m pytest tests/test_cre_ingest_history.py
  tests/test_cre_ingest_builders.py tests/test_om_parse.py -q`
  passed, 146 tests.
- Full collector Python suite:
  `python3 -m pytest tests/ -q` passed, 1,384 tests; 17 skipped.
- Collector TypeScript validation:
  `npm test` passed, including `tsc --noEmit` and 479 unit tests.
- `git diff --check` and `python3 -m py_compile cre_enrich.py om_parse.py`
  passed.
- Direct CLI checks confirmed `cre_enrich.py --om-parse` exits `2` and
  `om_parse.py --apply` exits `78`.
- Source drift scan found no remaining executable or source-contract
  four-column OM-facts target.
- Durable local PostgreSQL 17 proof:
  `bash tests/run_om_facts_postgres_contract.sh` passed. It applied
  `013_cre_listing_om_facts.sql`, executed the generated OM upsert three times,
  and asserted two parser-version rows, an updated `om-contract/1` row, and a
  coexisting `om-contract/2` row. The container was unexposed and removed.

## Decisions made

- `015_align_om_facts_conflict_key.sql` remains a guarded legacy migration. It
  is not part of this repair because the observed production index is already
  five-column.
- The standard collector suite remains pure-transform and no-network. The
  PostgreSQL behavior proof is opt-in, isolated, and never targets Supabase or
  the running Firecrawl queue database.

## Adversarial review follow-through

The dedicated skeptic pass confirmed four defects. All are corrected on this
branch: the executable Firecrawl OM writer is fail-closed, `cre_market_index`
ownership names GetCREdata, the root ownership pointer no longer claims an
unmerged local Context file, and the generated upsert has a durable PostgreSQL
17 contract command. The review also refuted three non-defects: the repaired
five-column target matches the source migration, the Firecrawl Linear IDs are
correct, and the failure-marker/webhook changes do not activate a scheduler.

## Remaining rollout gate

The branch must be reviewed and merged with Cayman approval. Only after the
actual Mac mini checkout runs this commit may an explicitly approved bounded
five-row enrich canary resume. The canary must retain the normal additive-only
behavior and verify zero released claims, zero constraint errors, no status or
soft-delete writes, and a fresh `ok:true` marker.

## Left to the operator

1. Review draft PR #22, including its local evidence and live-runtime gates.
2. Provide the literal merge approval required by the repository policy before
   merging into `main`.
3. After merge, restore the actual Mac mini runtime and credentials, deploy the
   merged commit to a non-TCC checkout, and separately authorize the five-row
   canary. The draft PR does not authorize any of these production actions.

## Live Mac mini preflight (2026-07-11)

Read-only access through the configured `mini` SSH alias found a different,
clean checkout at `/Users/cayman-mac-mini/Documents/GitHub/firecrawl` on
`cursor/gha-detach-fork-ci-53bb` (`3b2f803ea`). It does not contain this branch
and still emits the old four-column OM-facts conflict target. No
`ai.agentic.cre-*` launchd label, CRE run marker, collector artifact, or
collector log is present, so no job is currently available to pause or canary.

The local Firecrawl API is unavailable because the configured Colima Docker
daemon is stopped. The checkout is under `~/Documents`, which the collector
reports as a launchd TCC risk, and the read-only status command cannot discover
a POSTGRES_URL environment file. Restore the runtime and deploy the merged
branch to a verified non-TCC checkout before treating a five-row canary as
available.
