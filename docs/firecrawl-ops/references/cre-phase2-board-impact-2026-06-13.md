# Phase-2 Status Activation: Board Impact Analysis

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

- **Terminal (sold / leased / off_market): ~569 rows (0.8% of the board) correctly drop.** Pure accuracy win, happens under any gate option. These are rows currently shown as "active" that the source itself marks sold/leased.
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
