# CRE consolidation execution-status audit (2026-07-11)

**Plan:** `OPTIMAL_EXECUTION_PLAN_2026-07-11.md`
**Firecrawl branch:** `fix/cre-consolidation-safety` at `466cf5614`
**Scope:** Read-only audit plus locally verified, pushed preparation branches.
**Status:** The implementation-preparation phase is complete. The production
execution phase is not authorized or proven.

## Verified implementation preparation

### Firecrawl writer repair

- The OM-facts generated upsert uses the production five-column identity:
  `(listing_id, fact_group, fact_key, source_doc_url, parser_version)`.
- `cre_enrich.py` no longer exposes the retired `--om-parse` path, and
  `om_parse.py --apply` exits `78` before database or ingestion work.
- The failure-webhook URL is passed to curl through stdin configuration rather
  than process arguments. Its regression test also rejects newline injection.
- Latest clean-worktree verification at this branch head:
  - `python3 -m pytest tests/ -q`: 1,386 passed, 17 skipped.
  - `bash tests/run_om_facts_postgres_contract.sh`: passed against isolated
    PostgreSQL with the real five-column conflict target.
  - `npm ci && npm test`: typecheck passed and 479 unit tests passed.
  - `git diff --check origin/main...HEAD`: passed.

### Cross-repository preparation

- GetCREdata unattended hardening is pushed at `a2d55df`; current verification
  collected 62 tests, with 58 passed and 4 environment-gated skips.
- GetCREdata parser-version view preparation is pushed at `2ac4dd2`; current
  verification collected 55 tests, with 51 passed and 4 environment-gated
  skips.
- aa-hub's disabled GetCREdata scheduler preparation is pushed at `c638d8a`.
  Its local renderer dry-run and activation-document consistency checks pass;
  it remains disabled and unallowlisted.
- The Context Engineering ownership contract is version 2 at `a08d80f`.
  `ruby scripts/check_cre_data_object_ownership.rb` verifies that the observed
  property-type drift and adoption gates remain explicit.
- EQUIRE's proposed listing-market integration is pushed at `56b03de19`, with
  focused migration-contract tests and typecheck passing. It remains unapplied.

## Current live-environment evidence

The Mac mini is not in a state that permits a canary:

- Its Firecrawl checkout is at `~/Documents/GitHub/firecrawl` on
  `cursor/gha-detach-fork-ci-53bb` (`3b2f803ea`), not the safety branch. It
  still contains the legacy OM parser path.
- Colima is stopped, Docker is unavailable, and `http://localhost:3002` is
  unreachable.
- No CRE monitor, enrich, weekly, or daily launchd labels, plists, cron entries,
  status markers, logs, or shared lock exist. There is no current monitor-green
  proof and no loaded job to pause.
- The `~/Documents` checkout remains a launchd TCC-risk location.
- aa-hub is still on its old disabled GetCREdata stub: `default` environment,
  exit `78`, 3,600-second timeout, and 01:00 schedule. There is no dedicated
  hub environment file, allowlist entry, or GetCREdata run record.
- The Mac mini GetCREdata checkout is an older branch with no reviewed
  hardening code, virtual environment, configuration environment, cache, or
  logs. Free disk is approximately 18 GiB (1 percent of the data volume).

## Unmet plan gates

| Plan requirement | State | Required evidence before completion |
| --- | --- | --- |
| Wave 0 monitor-only containment | Unmet | Fresh monitor `ok:true` marker after runtime recovery. |
| Wave 1 deployed writer repair and canary | Unmet | Reviewed merge, verified Mac mini checkout/runtime, explicit five-row canary approval, then the required scheduled observation window. |
| Wave 2 ownership adoption | Unmet | AGENTIC-1233 owner acknowledgement and adopted versioned property-type crosswalk. |
| Wave 3 GetCREdata production proof | Unmet | Reviewed deployment, dedicated environment, PITR or snapshot proof, validation-only and supervised routine export evidence. |
| Wave 4 aa-hub activation | Unmet | Provisioned runtime, disk remediation, explicit activation approval, rendered job, first-run evidence, and seven-day proof. |
| Wave 5 observability | Unmet | Restored collector runtime, approved alert proof, enabled report-only health lane, and off-host alarm evidence. |
| Wave 6 EQUIRE product integration | Unmet | Adopted crosswalk, both canaries, named DDL approval, live-ledger proof, and product-path smoke evidence. |
| Waves 7 and 8 extraction and cleanup | Deferred | Explicit repository approval, both stable canaries, 30-day rollback window, and restore proof. |

## Required authority to resume live waves

1. The literal phrase `Cayman approved this merge` for Firecrawl PR #22.
2. Explicit approval for Mac mini runtime recovery, checkout relocation or
   TCC authorization, deployment, and the bounded production canary.
3. Cross-repository owner acknowledgement of the versioned property-type
   crosswalk in AGENTIC-1233.
4. Separate approval for aa-hub activation and EQUIRE production DDL after
   their documented preconditions are proven.

This audit does not authorize a production change, scheduler activation,
database write, repository creation, deletion, PR merge, or EQUIRE DDL.
