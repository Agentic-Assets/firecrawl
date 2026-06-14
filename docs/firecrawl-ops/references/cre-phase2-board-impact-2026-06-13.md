# Phase-2 Status Activation: Board Impact Analysis

> **PRE-2026-06-14 SNAPSHOT (stale totals, design still valid).** This analysis
> was computed against the board BEFORE the full `colliers-main` ingest landed:
> it assumes a 72,544-row board, the 943-row bounded `colliers-main` batch, and
> 254 passing tests. As of 2026-06-14 the board is ~87,328 active, `colliers-main`
> is COMPLETE (15,829 rows), and the suite is 261 pytest. The terminal-drop and
> under-contract/pending row counts below are therefore STALE and undercount the
> real impact; they are not authoritative until `phase2_derive.py` is re-run on
> the post-colliers-full board (the doc itself flags this in the Headline and the
> colliers-main caveat). Also note: status activation is now OPT-IN and default-OFF
> in `cre_ingest.py` (requires `--activate-status` / `CRE_ACTIVATE_STATUS=1`); it
> does NOT fire on the next ingest as the "Authoritative activation order" text
> below implies. The qualitative conclusions (low blast radius, Choice (a)
> COALESCE, Option B gate, the consumer-deploy-first ordering) still hold. Live
> board and per-source counts: `START_HERE.md`.

Read-only. Computed 2026-06-13 by running the production `norm_status` over the
freshest full-run artifact per source (grouped by `(sourceKey, external_id)` via
`to_row`, terminal-wins across sale+lease passes), cross-referenced against the
live prod active board (72,544 rows, read via Supabase MCP). Source data + the
re-runnable derivation script:
`tasks/tmp/scratch-cre-007-phase2-2026-06-13/` (`phase2_derive.py`,
`phase2_artifact_buckets.json`). Re-run `phase2_derive.py` after the
`colliers-main` full ingest lands to refresh the terminal-drop totals.

The 2026-06-13 `avison-young` monitor `--apply` seed was observe-only
(`cre_source_baseline`, `cre_source_index`); Phase-2 status activation in
`cre_ingest.py` and the EQUIRE board gate remain separate, gated work.

## Headline

Activating accurate status moves a small, well-understood slice of the board:

- **Terminal (sold / leased / off_market): ~569 rows (0.8% of the board) correctly drop** on the current board. Pure accuracy win, happens under any gate option. These are rows currently shown as "active" that the source itself marks sold/leased. Treat ~569 as a current-board floor: once the full `colliers-main` run ingests it rises to roughly ~1,567 (up to ~1,840 at the in-flight artifact's 8.5% terminal rate). The qualitative low-blast-radius conclusion holds at both ends of the range; only the number moves. Re-run `phase2_derive.py` after that ingest before quoting a number.
- **Under-contract + pending: ~905 rows (1.2%).** These drop too *unless* the board gate keeps them. This is the one real choice (Option A vs B below).
- **~71,070 rows (98%) are unaffected.** They carry no terminal/uc/pending signal, so they stay visible.

So the blast radius is tiny: even the most aggressive option moves ~2% of the board, and the unambiguous-win portion is ~0.8%. Status activation is low-risk.

## Per-source breakdown (prod-active scaled)

| Source | Prod active | Terminal (drop) | Under contract | Pending | Tier |
|---|--:|--:|--:|--:|---|
| cbre | 19,028 | 88 | 21 | 2 | null (text-only) |
| cushman-wakefield | 11,318 | 16 | 9 | 0 | native |
| jll | 10,741 | 0 | 3 | 0 | null (text-only) |
| lee-associates | 9,223 | 3 | 77 | 0 | native |
| svn | 5,287 | 24 | ~301 | 0 | native |
| newmark | 4,371 | 62 | 0 | 4 | null (text-only) |
| marcus-millichap | 3,124 | 12 | 0 | 0 | null (text-only) |
| avison-young | 2,201 | 0 | 0 | 0 | null |
| transwestern | 2,021 | 0 | 0 | 0 | null |
| cbre-dealflow | 1,836 | 2 | 207 | 0 | native |
| colliers | 1,172 | 281 | 65 | 0 | native |
| colliers-main | 943* | ~80* | ~2* | 0* | native |
| jll-investor | 934 | 1 | 214 | 0 | native |
| nai-global | 241 | 0 | 0 | 0 | native |
| savills | 104 | 0 | 0 | 0 | null |
| **Total** | **72,544** | **~569** | **~899** | **~6** | |

\* **colliers-main caveat:** prod has only the 943-row bounded batch, but the
full run artifact (12,726 rows, the in-flight ingest) shows **1,078 terminal
(8.5%) and 25 under-contract**. Once the full colliers-main run is ingested, its
terminal-drop alone jumps from ~80 to ~1,078. Re-run this analysis after that
ingest; the totals above will rise mostly from this source.

These counts are a **conservative floor**: `norm_status` only fires on explicit
signals. With monitor mode + detail enrichment, native-status sources will surface
more. Disappearance-only sources (cbre, jll, newmark, marcus, avison, transwestern,
savills) only get terminal/uc here from explicit phrases in the listing title.

## The robust activation design (two coupled choices)

### Choice 1: how to write status (eliminates the coverage cliff by construction)

The ~41.6k disappearance-only rows (57% of the board) have no source status field,
so `norm_status` returns NULL for them. There are two ways to wire activation:

- **(b) Direct write** `status = norm_status` → no-signal rows become NULL. This is
  what design §12.4 assumes, and it *requires* the board gate to add `OR status IS
  NULL` or ~41.6k rows silently vanish. One forgotten clause = mass outage.
- **(a) COALESCE non-null** `status = COALESCE(<terminal/uc/pending only>, status)`
  → status is only ever *upgraded* to a real signal; no-signal rows stay `'active'`.
  Status is never NULL, so the coverage cliff cannot happen.

**Recommendation: (a) COALESCE.** It is the robust choice: the 57% no-signal
majority can never be dropped by a gate mistake, terminal rows still correctly flip
and drop, and lifecycle for no-signal sources stays governed by disappearance/
`--mark-missing` (their intended mechanism), not by a NULL status. This is coherent
with the existing ingestor, which already treats a NULL `norm_status` as "no
opinion, never a downgrade."

### Choice 2: board gate for under-contract / pending

- **Option A:** gate stays effectively `status = 'active'`. Under-contract/pending
  (~905 rows) drop off the board along with terminal.
- **Option B (recommended):** gate becomes `status IN ('active','under_contract','pending')`.
  Under-contract/pending stay visible.

**Recommendation: Option B.** A property going under contract is one of the
highest-value signals for a deal-intelligence platform (imminent comp, active
broker, ownership about to change). Hiding ~900 such deals discards exactly the
intelligence EQUIRE exists to surface. It is a reversible one-clause widening.

### Combined recommended gate

With Choice (a) COALESCE, status is never NULL, so the gate is simply:

```sql
status IN ('active','under_contract','pending')
```

(The `OR status IS NULL` clause that §12.4 requires is only needed if Choice (b)
direct-write is used. Choosing COALESCE removes that fragility.)

## Exact consumer edit (gated to you; second repo + live board)

Repo `dynamically-display-cre-listing-data`:
- `lib/listing-filters.ts` (~line 129): the active-status predicate.
- `lib/db/credeals.ts` (~line 202): the `BOARD_STATS_QUERY` status filter.

Change both from an `'active'`-only check to `status IN ('active','under_contract','pending')`.
If Phase-2 activation instead writes NULL (Choice b), append `OR status IS NULL` to both.

This edit, the ingestor status-wiring, and the first live activation stay **gated for
your go-ahead**: they touch a second production repo and what live users see.

## Implementation status and activation runbook (2026-06-13)

The Track-2 observe-only seed and the Phase-2 code are in place; the live
activation and the consumer deploy stay gated for go-ahead.

**Done (verified):**
- **Gate-0 CLEARED.** Prod `cre_listings_status_check` already allows
  `under_contract`, `pending`, `sold`, `leased`, `off_market` (verified
  2026-06-13 on `fhqycqubkkrdgzswccwd`). No migration re-apply is needed; the
  Phase-2 targeted UPDATE cannot raise a check_violation.
- **T2.3 seed applied (observe-only).** `cre_gate.py --apply --update-baseline`
  then `cre_monitor.py --apply` on the all-source monitor artifact seeded
  `cre_source_baseline` (11 sources) and `cre_source_index` (73,693 rows) with
  0 events and 0 enrichment-queue rows. Board verified unchanged: 72,544 live
  active, 0 live non-active, 0 NULL status, 5,269 soft-deleted.
- **T3.1 wired and hardened** in `cre_ingest.py` (Choice (a) COALESCE):
  a terminal-stickiness guard (a sold/leased/off_market row is never downgraded
  to under_contract/pending by a cross-run re-signal) and a status-flip
  pre-flight (per-source `RAISE NOTICE` observability always on; optional
  circuit breaker via `CRE_STATUS_FLIP_MAX_FRACTION`, default OFF so the
  unattended daily ingest is never blocked). 254 pytest pass; the PL/pgSQL
  pre-flight block was validated live against the schema.
- **T3.2 consumer edit committed** to `dynamically-display-cre-listing-data`
  branch `feat/multi-source-live-listings` (not merged, not deployed). The
  adversarial sweep found six sites, not two; all are widened: the shared board
  gate (`lib/listing-filters.ts`), the stats count (`lib/db/credeals.ts`
  `BOARD_STATS_QUERY`), the on-market header stat and copy (`app/page.tsx`),
  the coverage-summary copy, the detail-page status badges
  (`property-detail.tsx`), and the test assertions. Typecheck, 50 tests, and
  lint are clean.

**Authoritative activation order (load-bearing, the one rule to not get wrong):**
1. **Deploy T3.2 first.** The widened `status IN (...)` predicate is provably a
   no-op until status data exists (the board is 100% active today), so it is the
   zero-risk, fully reversible half of the pair. Merge and deploy the consumer
   branch.
2. **Then activate T3.1.** It lands on the next daily ingest (or a manual
   `cre_ingest.py --apply` on a full, non-monitor artifact). For the first
   activation run, set `CRE_STATUS_FLIP_MAX_FRACTION` (e.g. `0.5`) so a source
   parsing regression aborts the whole run, and inspect the per-source
   `status-flip` NOTICE counts against the expected range before treating the
   run as healthy. Do NOT activate before step 1 deploys, or ~905
   under_contract/pending rows briefly drop off the active board with no UI
   trace or soft-delete event.

**005 views widened (prepared on branch; apply gated).** The four agent-facing
surfaces in `sql/005_cre_views.sql` (`v_cre_active_for_sale`,
`v_cre_active_for_lease`, `v_cre_market_summary`, and `search_cre_listings`, the
canonical EQUIRE agent search entry point) now filter
`status IN ('active','under_contract','pending')` instead of `status='active'`,
matching the Option B board gate so EQUIRE agents see under-contract / pending
deals. The views keep their historical `v_cre_active_for_*` names to preserve the
EQUIRE read contract; an authoritative header note in 005 records the on-market
semantics. The edit is on the feature branch only; the live `CREATE OR REPLACE`
apply stays **gated** (live DDL). Verified read-only 2026-06-13: the widened
predicate parses against the live schema and is a zero-row no-op today (72,544
active, 0 under_contract, 0 pending, 0 NULL), so it applies cleanly in the same
gated change set as T3.2 / T3.1. It is a display-layer no-op until T3.1 writes
status, so it must not be treated as the go-live lever on its own.

**Function body, not just views (best-practices review 2026-06-13, finding 1).**
`search_cre_listings()` carries the same widened predicate in its body. The live
function is still on `status = 'active'`; applying the view DDL alone does NOT
redeploy the function. The gated 005 apply MUST run the `CREATE OR REPLACE
FUNCTION` block, and the runbook must verify it immediately after:
`SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname='credeals' AND p.proname='search_cre_listings';`
and confirm the body contains `under_contract`. Otherwise EQUIRE agent searches
would silently exclude every under_contract / pending row even after activation.
The same 005 apply also refreshes `v_cre_listings_full` to pick up
`last_seen_at` / `source_lastmod` / `canonical_key`, which the live view is
currently missing because of a stale `l.*` (finding 2). Full review and
fresh-DB smoke-test evidence:
`scripts/firecrawl-ops/sql/advisor-reports/2026-06-13-cre-best-practices-review.md`.
