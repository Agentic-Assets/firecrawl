# CRE listing pipeline alignment with EQUIRE market context

**Baseline review date:** 2026-07-11

**Bottom line:** The systems are structurally aligned in most important ways,
but they are not yet aligned enough to enable an EQUIRE application or MCP
consumer without additional work. The EQUIRE branch preserves producer
ownership, joins the right listing and freshness identities, excludes
soft-deleted inventory, discloses fallback scope, and uses service-role-only
RPCs. The remaining gaps are material: a fresh-schema CBSA column collision,
an unresolved property-type crosswalk, an unapplied GetCREdata parser-version
fix, unbounded cache staleness after failures, a broken rollback order, and no
executable cross-repository PostgreSQL test.

## Current-state refresh (2026-07-15)

This section takes precedence over the historical branch snapshot below. The
contract direction remains sound, but the repository state changed materially
after 2026-07-11.

| Surface | Current remote evidence | Current conclusion |
| --- | --- | --- |
| Firecrawl | `fix/cre-consolidation-safety` is `5208335a`; `origin/main` is `c74ece4` | The eight Firecrawl safety findings remain open. No runtime code changed after the original review commit. |
| EQUIRE | `feat/cre-listing-market-context` was merged as [PR #418](https://github.com/Agentic-Assets/CRE_EQUIRE/pull/418); EQUIRE `main` is `a65389e` | The database read surface and performance caches are repository-recorded as applied. The new CBSA RPCs are still not called by an application or MCP runtime. The existing city/state market strip is a separate, fail-soft consumer and is not adoption of the new RPCs. |
| GetCREdata | `main` is `aa39939` and contains both the parser-version and unattended-hardening branches | The corrected parser-selection code is merged, but its revised `cre_market_index` definition is still repository-recorded as unapplied shared-schema DDL. EQUIRE caches therefore cannot yet prove they read the corrected producer definition. |
| Context Engineering | `docs/cre-data-object-ownership` is `417ae07`, 25 commits behind `main` (`6d760a1`) | The ownership manifest is proposed, unmerged, and not merge-ready. Its EQUIRE state and object inventory require correction before it can become canonical guidance. |

No new production query was run for this refresh. Statements that a migration,
cache, or cron job is applied remain **repository-recorded production evidence**,
not a fresh live-database assertion.

### Reconciled cross-repository conclusions

1. **EQUIRE DDL is no longer a future gate.** The EQUIRE migration, cache, and
   ACL evidence is recorded on `main`. Do not re-run or re-authorize it as if it
   were pending. Consumer adoption, source quality, and monitoring are still
   open work.
2. **The Firecrawl/EQUIRE view-shape conflict remains a live release hazard.**
   Firecrawl's generic runner applies `012` before recreating `005`, and `005`
   exposes `l.*` from `cre_listings`. That shape includes CBSA columns. EQUIRE's
   applied migration deliberately joins the base table because its recorded
   production `v_cre_listings_full` does not expose CBSA columns. Do not run the
   Firecrawl generic runner or recreate that view against the shared schema
   until an explicit compatibility migration and disposable-PostgreSQL proof
   show that dependent EQUIRE views remain valid.
3. **GetCREdata's parser fix is merged but not deployed to the shared producer
   view.** Review the intended DDL, capture before/after rows and pooled
   metrics, apply only with owner approval, then refresh and revalidate the
   EQUIRE caches.
4. **The property-type crosswalk remains the adoption gate.** AGENTIC-1233
   still needs an owner-approved, versioned mapping before any new consumer
   treats exact property-type matches as governed equivalence.
5. **There is no approved unattended scheduler path in these artifacts.**
   GitHub Actions stays manual-only. aa-hub is historical source and runbooks,
   not an execution control plane. A future scheduler needs a separate Cayman
   approval under the current coordinator policy.

## Reviewed state

The remainder of this section is the July 11 baseline record. Where it differs
from the July 15 refresh, the refresh is authoritative.

| Repository | Reviewed ref | State used in this review |
| --- | --- | --- |
| Firecrawl listing collector | `fix/cre-consolidation-safety` at `a7f4a0b8fa8b818e0c07218c94b341a08d15f7ad` | Historical baseline. See the current-state refresh for the current branch head. |
| EQUIRE | `feat/cre-listing-market-context` at `1c1f72cce23c169ded215fe56f070061bca1b7c5` | Historical baseline. The branch was subsequently merged into EQUIRE `main` as PR #418. |
| GetCREdata unattended hardening | `fix/getcredata-unattended-hardening` at `f1e98e24361444aa822c1c55cd64c46d1d9f2d87` | Historical baseline. The branch is now contained in GetCREdata `main`. |
| GetCREdata market-index parser fix | `origin/fix/cre-market-index-parser-version` at `2ac4dd2` | Historical baseline. The branch is now contained in GetCREdata `main`; shared-schema apply proof remains separate. |

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

### Historical concurrent edits observed after the branch baseline

After the baseline review and first Firecrawl report commit completed, the
EQUIRE checkout acquired uncommitted changes from another active agent. This
review did not modify, stage, commit, or push those files. They are not part of
EQUIRE commit `1c1f72cce` or its remote branch yet.

The local edits appear to address several review items:

- relabel the release runbook's pre-apply instructions as historical and state
  that all three migrations are applied;
- correct the compensating-migration dependency order and use the pg_cron job
  identifier for unscheduling;
- describe one-hour freshness as conditional on successful jobs rather than an
  unconditional bound;
- add current-state cache acceptance queries and service-role-RPC-only guidance;
- state in module guidance that no application or MCP consumer is wired yet.

The local edits do not address the fresh-schema CBSA collision, GetCREdata's
unmerged parser-version fix, the unresolved property-type crosswalk, automatic
producer and cache freshness monitoring, or the missing executable PostgreSQL
integration harness. The edited forward queue now says to wire an application
and MCP consumer because DDL evidence is complete, but it still does not require
AGENTIC-1229, AGENTIC-1230, and AGENTIC-1233 before consumer reliance.

This was an accurate July 11 handoff boundary. The EQUIRE feature was later
merged to `main`; use the July 15 refresh rather than this historical local
worktree state when deciding the next action.

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

### 2. P1 historical finding: the EQUIRE DDL state required reconciliation

At the baseline, the EQUIRE branch recorded all three migrations as applied
through an explicit operator exception while Firecrawl documentation still
described the migration as future work. EQUIRE has since merged the feature
into `main` and retains the applied-state evidence there.

**Current recommendation:** Reconcile the Firecrawl audit, operator runbook,
and AGENTIC-1232 with EQUIRE `main`'s repository-recorded ledger state. Preserve
AGENTIC-1229, AGENTIC-1230, and AGENTIC-1233 as gates for producer recovery,
data quality, and product reliance. They are not retroactive gates for DDL
already recorded as applied.

The EQUIRE-side documentation was subsequently merged. Firecrawl and Linear
reconciliation remain required.

### 3. P1: the current EQUIRE consumer-forward language can bypass producer gates

EQUIRE's original release runbook requires the property-type crosswalk and
both producer canaries before product reliance. Its later closeout records that
DDL was applied ahead of those gates. The forward queue now says to wire an app
or MCP consumer after the views are live and verified, which can be read as
permission to proceed because the views and caches are recorded as live.

**Recommendation:** Make the consumer gate explicit. The repository can safely
land fail-soft adapters and tests, but do not enable product reliance until
Firecrawl's listing canary, GetCREdata's supervised export and approved
coordinator observation window, and AGENTIC-1233's crosswalk adoption are all
recorded. Keep the existing RPCs dark and fail-open until then.

The concurrent EQUIRE forward-queue edit correctly states that DDL evidence is
complete and no consumer exists, but it now presents consumer wiring as the next
step without naming these producer and crosswalk gates. Add them before that
edit is committed.

### 4. P1: GetCREdata's parser-version repair is merged but not applied to the shared producer view

GetCREdata `main` now contains the parser-version repair, which selects one
latest `parser_version` per listing before pivoting OM facts. The shared
`cre_market_index` producer view is still repository-recorded as using the
older definition until separately approved DDL applies the revision.

EQUIRE caches the current producer view. Hourly refresh does not correct a
producer definition that combines revisions.

**Recommendation:** Review the now-merged GetCREdata implementation, then
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

The concurrent EQUIRE documentation patch corrects the unconditional wording,
but it does not add the automatic monitor or consumer age enforcement.

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

The concurrent EQUIRE patch now uses this dependency order and unschedules by
job identifier. The fix remains uncommitted and has not been executed against a
disposable database.

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

The concurrent EQUIRE patch narrows the recipe to producer-owned objects and
explicitly excludes the new views and caches. It should also require producer
owner approval before granting browser roles direct access to
`cre_market_index`.

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
4. Review the merged GetCREdata parser-version repair and separately approve
   application of its revised producer DDL.
5. Adopt the versioned property-type crosswalk through AGENTIC-1233.
6. Prove the Firecrawl listing canary and GetCREdata supervised export plus the
   observation window on the explicitly approved coordinator.
7. Install cross-repository freshness monitoring for producer state, cache
   jobs, cache timestamps, and market `as_of` values.
8. Add the disposable PostgreSQL integration harness.
9. Only then wire the EQUIRE application and MCP consumer with fail-open,
   scope-visible behavior.

## Verification performed in this review

- Confirmed all reviewed local checkouts were clean and matched the named
  branch refs at the baseline. The EQUIRE checkout later acquired preserved,
  uncommitted edits from another active agent, as documented above.
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
