# OM-facts writer repair closeout (2026-07-11)

**Branch:** `fix/cre-consolidation-safety`  
**Base:** `origin/main` at `c74ece496`  
**Implementation commit:** `477331e70` (`fix: align OM facts upsert contract`)  
**State:** pushed to `origin/fix/cre-consolidation-safety`. No PR, production
DDL, launchd change, collector run, or production database write was performed.

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

## Verification

- Focused Python contracts:
  `python3 -m pytest tests/test_cre_ingest_history.py
  tests/test_cre_ingest_builders.py tests/test_om_parse.py -q`
  passed, 146 tests.
- Full collector Python suite:
  `python3 -m pytest tests/ -q` passed, 1,394 tests; 17 skipped.
- Collector TypeScript validation:
  `npm test` passed, including `tsc --noEmit` and 479 unit tests.
- `git diff --check` and `python3 -m py_compile
  scripts/firecrawl-ops/cre_collector/cre_ingest.py` passed.
- Source drift scan found no remaining executable or source-contract
  four-column OM-facts target.
- Disposable local PostgreSQL 17 proof: applied `013_cre_listing_om_facts.sql`,
  executed the generated OM upsert three times, and observed two rows:
  `om-parse/1=1:0.9, om-parse/2=3:0.8`. This proves parser releases coexist and
  a same-release reparse updates in place. The container was unexposed and
  removed after the check.

## Decisions made

- `015_align_om_facts_conflict_key.sql` remains a guarded legacy migration. It
  is not part of this repair because the observed production index is already
  five-column.
- The standard collector suite remains pure-transform and no-network. The real
  PostgreSQL behavior proof was run manually in an isolated disposable
  container, not against Supabase or the running Firecrawl queue database.

## Remaining rollout gate

The branch must be reviewed and merged with Cayman approval. Only after the
actual Mac mini checkout runs this commit may an explicitly approved bounded
five-row enrich canary resume. The canary must retain the normal additive-only
behavior and verify zero released claims, zero constraint errors, no status or
soft-delete writes, and a fresh `ok:true` marker.
