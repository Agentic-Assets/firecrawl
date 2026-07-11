# CRE listing pipeline alignment with EQUIRE market context

**Review date:** 2026-07-11

**Bottom line:** The systems are structurally aligned in most important ways,
but they are not yet aligned enough to enable an EQUIRE application or MCP
consumer without additional work. The EQUIRE branch preserves producer
ownership, joins the right listing and freshness identities, excludes
soft-deleted inventory, discloses fallback scope, and uses service-role-only
RPCs. The remaining gaps are material: a fresh-schema CBSA column collision,
an unresolved property-type crosswalk, an unapplied GetCREdata parser-version
fix, unbounded cache staleness after failures, a broken rollback order, and no
executable cross-repository PostgreSQL test.

## Reviewed state

| Repository | Reviewed ref | State used in this review |
| --- | --- | --- |
| Firecrawl listing collector | `fix/cre-consolidation-safety` at `a7f4a0b8fa8b818e0c07218c94b341a08d15f7ad` | Clean, matches its remote branch |
| EQUIRE | `feat/cre-listing-market-context` at `1c1f72cce23c169ded215fe56f070061bca1b7c5` | Clean, matches `origin/feat/cre-listing-market-context`, fully contains current `origin/main` |
| GetCREdata unattended hardening | `fix/getcredata-unattended-hardening` at `f1e98e24361444aa822c1c55cd64c46d1d9f2d87` | Clean local review clone |
| GetCREdata market-index parser fix | `origin/fix/cre-market-index-parser-version` at `2ac4dd2` | Pushed, but not merged into `origin/main` and not recorded as applied |

The EQUIRE branch records that migrations `20260711120000`,
`20260711160000`, and `20260711170000` were applied to production and that two
pg_cron cache refresh jobs are active. This review verified those claims
against the branch's migration ledger, closeout, and runbook artifacts. It did
not independently query the live database or scheduler again.

The EQUIRE branch contains the database read surface only. No application or
MCP call site currently invokes `get_listing_with_market_context` or
`get_cre_market_summary_by_cbsa` outside migrations, docs, and contract tests.
That separation is useful because the remaining producer and governance gaps
can be closed before product exposure.

## What aligns correctly

### Ownership boundaries

- Firecrawl owns listing collection, `cre_listings`, `cre_source_index`, and
  listing-side schema migrations.
- GetCREdata owns market ingestion and the producer definition of
  `cre_market_index`.
- GetCREdata is the intended production OM extraction writer.
- EQUIRE owns only its product views, RPCs, consumer indexes, materialized
  caches, grants, and access posture.
- The EQUIRE migrations do not update listing rows, OM facts, or the
  GetCREdata producer view.

This is the right architecture. EQUIRE is consuming and governing data rather
than becoming another producer.

### Listing and freshness identities

EQUIRE joins `cre_source_index` on `(brokerage_id, external_id)`, which is the
same unique identity Firecrawl declares. It reads `last_enumerated_at` and
`soft_deleted`, rather than using listing `updated_at` as a freshness proxy.
That is the correct choice for an enumeration-driven listing system.

The detail RPC and summary view exclude database-soft-deleted listings. The
summary's on-market count uses `active`, `under_contract`, and `pending`, which
matches the widened Firecrawl status contract. Current Firecrawl status
activation remains separately gated, but EQUIRE is ready for the expanded
values when that gate is eventually approved.

### CBSA identity and fail-open behavior

Both systems use five-character text CBSA codes. EQUIRE uses an exact CBSA and
property-type match first, then an explicit CBSA-wide `all` fallback. Missing
context remains visible as `unavailable`; it does not fabricate a metric.

The branch's recorded apply evidence reports zero malformed CBSA codes and a
scope split of 97.5 percent exact, 2.2 percent CBSA-wide fallback, and 0.2
percent unavailable among listings with CBSA and property type. Those figures
are branch-recorded production evidence, not a new query from this review.

### Access posture and performance layer

The product views use `security_invoker`, direct application-role reads are
revoked, the RPCs fix `search_path`, and only `service_role` receives execute
permission. Independent source review found no obvious grant or RLS bypass.

The EQUIRE-owned materialized caches avoid repeatedly evaluating the expensive
GetCREdata view and listing summary. They expose cache snapshot timestamps and
do not modify the producer definition. Database pg_cron refresh is distinct
from GitHub Actions and does not conflict with the no-GitHub-Actions scheduler
decision.

## Issues and recommendations

### 1. P1: a fresh Firecrawl schema and the EQUIRE migration disagree on CBSA columns

Firecrawl migration `012` adds `cbsa_code` and `cbsa_name` to
`cre_listings`. Its master runner applies `012` before recreating migration
`005`, whose `v_cre_listings_full` uses `SELECT l.*`. A fresh Firecrawl schema,
or a later recreation of `005`, therefore exposes both CBSA columns through
`v_cre_listings_full`.

EQUIRE's `20260711120000` migration assumes the deployed view lacks those
columns. It selects `l.*` and then appends `base.cbsa_code` and
`base.cbsa_name`. PostgreSQL rejects duplicate view output-column names. The
production apply could succeed because the deployed Firecrawl view appears to
have frozen its `l.*` expansion before migration `012` added the columns. That
historical shape is not a portable producer contract.

**Recommendation:** Replace the EQUIRE `l.*` projection with an explicit,
non-overlapping column list or add a compatibility migration that selects one
CBSA source based on the producer view version. Add a disposable PostgreSQL
test that applies the current Firecrawl migration sequence first and then all
three EQUIRE migrations.

### 2. P1: the repositories disagree about whether the EQUIRE DDL is still future work

The EQUIRE branch records all three migrations as applied through an explicit
operator exception. Firecrawl's `2026-07-11-execution-status-audit.md` still
says the EQUIRE migration is unapplied, and its operator runbook still treats
AGENTIC-1232 as a future DDL request after producer canaries and crosswalk
approval.

**Recommendation:** Reconcile the Firecrawl audit, operator runbook, and
AGENTIC-1232 with the EQUIRE-recorded ledger state. Preserve AGENTIC-1229,
AGENTIC-1230, and AGENTIC-1233 as gates for producer recovery, data quality,
and product reliance. They are no longer retroactive gates for DDL that the
EQUIRE branch says is already applied.

### 3. P1: the current EQUIRE consumer-forward language can bypass producer gates

EQUIRE's original release runbook requires the property-type crosswalk and
both producer canaries before product reliance. Its later closeout records that
DDL was applied ahead of those gates. The forward queue now says to wire an app
or MCP consumer after the views are live and verified, which can be read as
permission to proceed because the views and caches are recorded as live.

**Recommendation:** Make the consumer gate explicit. Do not wire the app or MCP
surface until Firecrawl's listing canary, GetCREdata's supervised export and
aa-hub observation window, and AGENTIC-1233's crosswalk adoption are all
recorded. Keep the existing RPCs dark and fail-open until then.

### 4. P1: GetCREdata's parser-version repair is not in the active producer

The GetCREdata `cre_market_index` definition on its parser-fix branch correctly
selects one latest `parser_version` per listing before pivoting OM facts. The
default branch can aggregate facts across parser revisions with independent
`max()` expressions, allowing cap rate, NOI, occupancy, size, and asking price
from different parser runs to form one synthetic observation.

EQUIRE caches the current producer view. Hourly refresh does not correct a
producer definition that combines revisions.

**Recommendation:** Review and merge the GetCREdata parser-version branch, then
apply the revised producer view only through explicit shared-schema DDL
approval. Compare counts and pooled metrics before and after. Decide separately
whether one parser revision may aggregate multiple source documents or must
also select one document revision.

### 5. P2: the property-type contract remains unresolved

Firecrawl normalizes `warehouse`, `flex`, manufacturing, distribution, and
logistics into `industrial`, while GetCREdata can preserve `warehouse` as a
market property type. Other taxonomy boundaries, such as hospitality versus
hotel and self storage versus special purpose, also need one versioned rule.

The current exact-then-`all` logic is safe in the sense that it does not
silently translate one type into another. The branch records one current
warehouse or industrial exception that receives CBSA-wide context. It is still
not a settled cross-repository contract.

**Recommendation:** Complete AGENTIC-1233 with a versioned mapping, owner
acknowledgement, backfill policy, exact-match behavior, fallback behavior,
refresh procedure, and rollback. Require consumers to display
`market_metric_scope` and never label `cbsa_all` values as property-type
metrics.

### 6. P2: cache timestamps do not prove producer freshness

The caches retain the last good snapshot when a refresh fails. This is the
right corruption-avoidance behavior, but it means the data can become
arbitrarily old. `market_snapshot_refreshed_at` and
`summary_snapshot_refreshed_at` disclose cache refresh times; they do not prove
that Firecrawl enumeration or GetCREdata ingestion is current. A successful
hourly refresh can also copy a stale producer snapshot.

**Recommendation:** Treat one hour as a target SLO, not a hard bound, until an
automated monitor exists. Alert when either pg_cron job fails, either cache is
older than two hours, snapshot order is unexpected, Firecrawl source markers
are stale, or GetCREdata's last successful export exceeds its approved
cadence. At the consumer boundary, return stale or unavailable context when the
age policy fails.

### 7. P1: the documented compensating migration has the wrong dependency order

The rollback template drops `v_cre_cbsa_market_summary` before dropping
`cre_cbsa_market_summary_cache`, even though the materialized cache depends on
that view. PostgreSQL will reject the view drop and abort the transaction,
leaving the EQUIRE surface and cron jobs in place during an incident.

**Recommendation:** In a new forward-only compensating template, drop the RPCs
first, then `cre_cbsa_market_summary_cache`, then the two product views, then
`cre_market_index_cache` and the supporting index. Unschedule both jobs and
verify object absence, preserved producer objects, job absence, and migration
ledger continuity. Exercise the complete rollback in disposable PostgreSQL.

### 8. P2: tests prove SQL text, not database behavior

The focused EQUIRE test reads migration and runbook files and checks strings
and regular expressions. It does not execute PostgreSQL. It therefore cannot
catch the fresh-schema CBSA collision, rollback dependency failure, real role
privileges, SECURITY DEFINER behavior, concurrent-refresh eligibility,
duplicate fan-out, or stale-source behavior.

**Recommendation:** Add a local disposable-PostgreSQL contract harness. Apply
representative Firecrawl producer migrations, seed exact, fallback, missing,
stale, and soft-deleted rows, apply the three EQUIRE migrations, exercise both
RPCs under the relevant roles, refresh both caches, and execute the corrected
compensating migration. Keep this local and do not add GitHub Actions.

### 9. P2: current documentation can broaden access on a producer-owned view

`docs/supabase/cre-market-data-schema.md` still suggests direct `anon` and
`authenticated` grants on GetCREdata's shared `cre_market_index` view. The new
market-context surface deliberately uses service-role-only EQUIRE RPCs and
denies direct view access.

**Recommendation:** Remove the generic direct-grant recipe or mark it
historical and unauthorized. Any public or authenticated product read should
go through a reviewed least-privilege EQUIRE RPC after producer-owner review.

### 10. Boundary: no product consumer is implemented yet

The EQUIRE feature branch creates and documents the database surface, but no
application route, server action, tool, or MCP handler calls the new RPCs. This
is not a defect in the database feature. It is an important state boundary:
the data layer is applied according to branch evidence, while product rollout
has not occurred.

**Recommendation:** Keep this separation until the preceding gates are closed.
When wiring begins, use a server-side service-role path, enforce snapshot and
producer freshness, preserve the existing board/feed behavior when context is
unavailable, display metric scope and timestamps, and add product-level tests.

## Recommended execution order

1. Fix the eight Firecrawl review findings in
   `2026-07-11-eight-actionable-review-findings.md`.
2. Reconcile the recorded EQUIRE apply state in Firecrawl docs and Linear.
3. Correct and test the EQUIRE compensating migration and fresh-schema CBSA
   compatibility.
4. Merge and separately approve application of GetCREdata's parser-version
   repair.
5. Adopt the versioned property-type crosswalk through AGENTIC-1233.
6. Prove the Firecrawl listing canary and GetCREdata supervised export plus
   aa-hub observation window.
7. Install cross-repository freshness monitoring for producer state, cache
   jobs, cache timestamps, and market `as_of` values.
8. Add the disposable PostgreSQL integration harness.
9. Only then wire the EQUIRE application and MCP consumer with fail-open,
   scope-visible behavior.

## Verification performed in this review

- Confirmed all reviewed local checkouts were clean and matched the named
  branch refs.
- Confirmed EQUIRE's feature branch contains current `origin/main` and is 12
  commits ahead.
- Ran EQUIRE's focused migration contract: `17 passed`.
- Ran focused Firecrawl geo, source-registry, ingest-builder, and retired-OM
  tests: `127 passed`.
- Compared Firecrawl migrations `005`, `007`, `012`, `013`, `014`, and `015`
  with the three EQUIRE market-context migrations.
- Compared GetCREdata's default market-index definition with
  `origin/fix/cre-market-index-parser-version`.
- Used independent producer, consumer, documentation, and safety review passes,
  then retained only confirmed issues.

No production database, scheduler, application deployment, or CRE data was
changed by this review.
