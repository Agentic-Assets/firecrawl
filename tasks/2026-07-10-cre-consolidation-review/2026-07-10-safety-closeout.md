# CRE consolidation safety closeout (2026-07-10)

**Branch:** `fix/cre-consolidation-safety`
**Base:** `origin/main` at `c74ece496`
**Implementation commit:** `33ad9a4ea`
**State:** local hardening committed. No PR opened. Production DDL, launchd
changes, and production collection writes remain unperformed.

## Goal

Close the review's immediate safety gaps without applying production schema
changes or changing the scheduler: verify the shared OM-facts contract, restore
the local Firecrawl runtime, and make future tier failures observable.

## What shipped

- `33ad9a4ea` adds consecutive-failure markers and an optional, non-blocking
  `CRE_ALERT_WEBHOOK_URL` notification path in
  `scripts/firecrawl-ops/cre_collector/launchd/cre_run_tier.sh`.
- `cre_status.sh` now reports the persisted failure streak and escalates at two
  consecutive failures.
- `013` now declares the canonical five-column OM-facts key. New guarded
  `015_align_om_facts_conflict_key.sql` leaves an already-correct production
  index untouched and is staged, not applied.
- `CREDEALS_OWNERSHIP.md` establishes the proposed shared-object contract and
  records the observed GetCREdata dependency.
- `om_parse.py` is documented as a disabled fallback while GetCREdata is the
  current external OM-facts writer.

## Evidence

- Read-only production check: 398,040 `cre_listing_om_facts` rows; production
  already has `(listing_id, fact_group, fact_key, source_doc_url,
  parser_version) NULLS NOT DISTINCT`.
- Runtime proof after starting OrbStack and Docker Compose:
  `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh` passed, including API
  and scrape smoke checks.
- Focused Python verification passed:
  `python3 -m pytest tests/test_daily_scripts.py tests/test_shell_scripts_syntax.py tests/test_om_parse.py -q` (41 passed).
- TypeScript verification passed after local dependency installation:
  `npm test` (typecheck plus 479 unit tests).
- Full Python suite result: 1,356 passed, 30 failed, 17 skipped. All failures
  are pre-existing ZIP/CBSA fixture failures because the clean branch lacks the
  expected mini crosswalk fixture. They do not exercise this commit.

## Decisions recorded

- Production is already on the five-column OM-facts index. The repository DDL
  was stale, so the implementation corrects source and stages a guarded legacy
  migration instead of applying DDL.
- GetCREdata remains a separate system. The contract consolidates schema
  ownership and compatibility rather than merging repositories.
- The restored runtime is proven healthy, but no manual enrich or collection
  run was triggered because it would write production listing data.

## Operator decisions still required

1. Approve or revise the ownership contract with GetCREdata's owner.
2. Approve a maintenance plan if any legacy database needs migration `015`.
3. Choose the GetCREdata scheduler lane, aa-hub or GitHub Actions, and provide
   the required credentials and execution owner.
4. Authorize any launchd cutover, production tier run, new `cre-listings`
   repository, or production view/DDL work.
