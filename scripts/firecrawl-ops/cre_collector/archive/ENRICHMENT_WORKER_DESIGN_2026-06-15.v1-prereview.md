# Tier-B Enrichment Worker + Cadence Restructure (Design)

> Status: DESIGN (not yet implemented). Author date: 2026-06-15.
> Companion executable plan: `workflows/cre_enrichment_worker.workflow.js`.
> Owner sign-off required before the live launchd cutover (Section 9).

## 1. Why

Today there are two collection paths (`collect.ts`):

- **Monitor path** (`--monitor`): cheap enumeration only (list page / internal
  JSON API / sitemap), emits index-level fields, skips detail-page render. Runs
  every 3h, diffs against `cre_source_index`, records new/changed/disappeared
  events, and **enqueues** new/changed listings into `cre_enrichment_queue`.
- **Full path** (no flag): enumeration **plus** a Firecrawl detail render per
  listing (price, sqft, cap rate, contacts, documents, images). Runs nightly
  (06:30) across all ~60k listings. This is the expensive job.

The gap: nothing drains `cre_enrichment_queue`. So the only thing that refreshes
*detail* is the nightly full re-scrape of everything. That is wasteful (we
re-render ~60k pages to catch the few hundred that changed) and slow.

This design closes the loop the monitor already opened, and rebalances cadence:

- Build the **enrichment worker** that drains the queue and scrapes ONLY the
  listings the monitor flagged as new/changed.
- **Monitor** drops from every-3h to **twice daily**.
- The **full re-scrape** moves from **daily to weekly** (it becomes the
  reconcile + detail-refresh safety net, and stays the only soft-delete tier).
- The heavy **daily tier is retired**; monitor (2x/day) + enrich (frequent)
  replace its freshness role at a fraction of the cost.

Target steady state: detail changes reach the board within hours (monitor
detection latency) instead of up to 24h, and per-day Firecrawl detail renders
drop from ~60k to the few hundred that actually changed.

## 2. Target tier model

| Tier | Schedule (local) | Action | Soft-delete? |
|------|------------------|--------|--------------|
| **monitor** | 2x/day (06:10, 18:10) | `collect --monitor` -> `cre_monitor.py --apply`: enumerate all, detect new/changed/disappeared, enqueue new/changed | No |
| **enrich** (NEW) | every 4h (00:30, 04:30, 08:30, 12:30, 16:30, 20:30) | `cre_enrich.py`: claim a batch from `cre_enrichment_queue` -> `collect --enrich-input` (targeted detail) -> `cre_ingest.py --no-mark-missing` -> mark done/retry | No |
| **weekly** | Sun 03:00 | `collect` (full) -> `cre_gate.py --strict` -> `cre_ingest.py --mark-missing` | **YES (only tier, triple-gated)** |
| ~~daily~~ | retired | unloaded; template kept for rollback | n/a |

Enrich is offset 30 min from the top of the hour so it never collides with the
monitor (06:10/18:10). The shared `mkdir` lock (`out/daily/.cre.lock`) is the
backstop: if two tiers ever overlap, the holder wins and the competitor exits 0.

### Soft-delete invariant (preserved, non-negotiable)

Only the **weekly** tier passes `--mark-missing`, and even it is triple-gated:

1. **Dispatcher gate** (`cre_run_tier.sh`): only the `weekly` branch passes
   `--mark-missing`; `enrich` and the retired `daily` never do.
2. **Coverage gate** (`cre_daily_update.sh`): `cre_gate.py --strict` must return
   0; any source "hold" auto-downgrades that run to `--no-mark-missing`.
3. **Ingest eligibility** (`cre_ingest.py`): per-brokerage `errors==0 &&
   staged>=floor && complete_folded_coverage` before any soft-delete.

The enrich worker is additive **by construction**: it always calls
`cre_ingest.py --no-mark-missing`, never activates status (status activation
stays default-off), and feeds only the claimed listings. It cannot soft-delete
or flip board state. Moving the full re-scrape from daily to weekly does NOT
move soft-delete authority, because weekly already owns it.

## 3. Component 1 - `collect.ts --enrich-input` (targeted detail)

### Problem
`collect.ts` today has no per-listing mode: each source is a monolithic
`src<Name>(tx, max, monitor)` that enumerates then enriches as one loop. The
worker needs to scrape an arbitrary set of specific listings.

### Design
Add a new mode gated entirely on a new flag, leaving the full/monitor paths
byte-identical when absent:

```
npx tsx collect.ts --enrich-input=<claim.json> --out=<enriched.json>
```

`claim.json` is the worker's claimed batch:
```jsonc
{ "items": [ { "sourceKey": "colliers-main", "externalId": "main:usa12345",
              "url": "https://www.colliers.com/en/properties/.../usa12345",
              "transaction": "sale" }, ... ] }
```

`collect.ts` groups items by `sourceKey`, dispatches each group to a registered
**SourceEnricher**, and emits the standard artifact (`{ runMeta:{mode:"enrich"},
sources, listings, brokers, totalListings }`) that `cre_ingest.py` already
consumes. No ingest change required.

### SourceEnricher interface (new, in `lib/enrich.ts`)
```ts
export type EnrichItem = { sourceKey: string; externalId: string; url: string;
                           transaction?: "sale" | "lease" };
export interface SourceEnricher {
  // Scrape + parse the given listings' detail pages into standard listing rows.
  // Returns one normalized listing per successfully enriched item; omit on
  // unrecoverable per-item failure (worker leaves those queued for retry).
  enrich(items: EnrichItem[]): Promise<any[]>;
}
export const ENRICHERS: Partial<Record<SourceKey, SourceEnricher>> = { ... };
```

### Per-source enrichers (phased)
- **Phase 1 (this work): the three highest-volume detail sources.**
  - `colliers-main`: already factored. Reuse exported
    `scrapeColliersMainDetailDoc(url)` + `parseColliersMainDetail(entry, doc)`
    (`sources/colliers-main.ts:62/215`). This is the reference implementation.
  - `jll-investor`: detail render -> extract from `__NEXT_DATA__`. Factor the
    existing inline detail parse into an exported `parseJllInvestorDetail`.
  - `cbre`: per-listing internal JSON API fetch. Factor the inline detail fetch
    into an exported `fetchCbreDetail`.
- **Generic fallback enricher** (`lib/enrich.ts`): for any source without a
  bespoke enricher, scrape the URL via `lib/scrape.ts` and run the shared
  JSON-LD / metadata extractors in `lib/html.ts`. Best-effort: captures
  price/status/description where the page exposes JSON-LD. Logged as
  `enricher=generic` so coverage is visible.
- **Sources with neither bespoke nor useful generic extraction**: the enricher
  returns `[]` for those items; the worker leaves them queued and they are
  refreshed by the **weekly full scrape** (safe degradation, logged).

Phase 1 deliberately covers the three sources that carry the most detail churn;
the rest ride the weekly backstop until their enrichers are added (tracked as
follow-up, Section 11). No listing is ever lost: anything the worker cannot
enrich is simply refreshed weekly, exactly as today.

## 4. Component 2 - `cre_enrich.py` (queue worker / orchestrator)

New script mirroring `cre_monitor.py` / `cre_ingest.py` conventions (argparse,
`load_db_url(CRE_ENV_FILE)`, psql shell-out; never prints the URL).

### Flow
1. **Claim** a batch (default 200) transactionally:
   ```sql
   WITH claimed AS (
     SELECT id FROM credeals.cre_enrichment_queue
     WHERE done_at IS NULL AND attempts < 5
       AND (claimed_at IS NULL OR claimed_at < now() - interval '1 hour')
     ORDER BY priority, enqueued_at
     LIMIT :batch
     FOR UPDATE SKIP LOCKED
   )
   UPDATE credeals.cre_enrichment_queue q
     SET claimed_at = now(), attempts = q.attempts + 1
     FROM claimed WHERE q.id = claimed.id
   RETURNING q.id, q.source_key, q.external_id, q.url, q.reason;
   ```
   - `claimed_at < now() - 1h` reclaims rows from a crashed prior run.
   - `attempts < 5` skips dead-lettered rows.
   - `FOR UPDATE SKIP LOCKED` is defensive; the `mkdir` lock already serializes
     tiers, so concurrent workers should not occur.
2. If zero claimed: log "queue empty", write marker, exit 0 (cheap no-op).
3. Write claimed rows to `out/enrich/claim_<stamp>.json`.
4. `npx tsx collect.ts --enrich-input=claim.json --out=enriched.json`.
5. `python3 cre_ingest.py --in enriched.json --no-mark-missing` (status
   activation OFF by default; the `CRE_STATUS_FLIP_MAX_FRACTION` breaker is inert
   while status activation is off, so a small batch cannot trip it).
6. **Complete**: by `(brokerage_id, external_id)`,
   - external_ids present in `enriched.json` -> `SET done_at = now()`.
   - claimed-but-absent (scrape failed) -> leave `done_at` NULL; `attempts` was
     already incremented at claim, so they retry next run until `attempts >= 5`,
     then they fall out of the drain set (dead-letter) and surface in
     `v_cre_enrichment_dead` for inspection. Set `last_error` from the run.

### Safety properties
- Always `--no-mark-missing`; cannot soft-delete.
- Never `--activate-status`; cannot flip board state.
- Partial-artifact safe: `cre_ingest.py` upsert is keyed on
  `(brokerage_id, external_id)`; L1 COALESCE-keep means a transient parse miss
  never blanks a prior good price; the M1 folded-coverage guard blocks
  mark-missing on partial data anyway (and mark-missing is not passed).
- Idempotent: re-running a claim is safe (upsert + `done_at` set-once); the
  queue unique key `(brokerage_id, external_id, reason)` dedups enqueues.

## 5. Component 3 - cadence restructure (launchd)

Files to change (all citations from the scout pass):
- `launchd/ai.agentic.cre-monitor.plist.template:33-83` - replace the 8-entry
  `StartCalendarInterval` array with two entries (06:10, 18:10).
- `launchd/ai.agentic.cre-enrich.plist.template` (NEW) - 6-entry array
  (00:30/04:30/08:30/12:30/16:30/20:30); `EnvironmentVariables` = `PATH` +
  `__ENV_EXTRA__` (CRE_ENV_FILE). No `CRE_MONITOR_APPLY`, no status-flip var.
- `launchd/install_launchd.sh` - add `enrich` to the TIERS list, `label_for`,
  and rendering; keep `--load` gating. Add a guard so `--load` never loads
  `weekly` implicitly.
- `launchd/cre_run_tier.sh:207+` - add an `enrich)` case: `python3 cre_enrich.py
  --batch "${CRE_ENRICH_BATCH:-200}"`. Daily branch is retired (leave the case
  for rollback but it is no longer scheduled).
- `cre_status.sh` - add `enrich` to `stale_threshold` (1.5x cadence = 6h),
  `newest_artifact` (out/enrich), and the three `for tier in ...` loops
  (`:103/130/288`); update the monitor stale threshold from 4.5h to 18h
  (1.5 x 12h).
- Retire daily: `launchctl unload` its plist at cutover (Section 9); keep
  `ai.agentic.cre-daily.plist.template` for rollback.

The weekly template and its `--mark-missing` dispatch are **unchanged**; weekly
simply becomes the sole full-scrape carrier.

## 6. Component 4 - SQL (migration `010`, additive only)

`sql/010_cre_enrichment_ops.sql` (new), idempotent, `credeals` schema:
- `v_cre_enrichment_queue_pending` = `done_at IS NULL AND attempts < 5`
  (priority, enqueued_at order) - operational visibility.
- `v_cre_enrichment_dead` = `done_at IS NULL AND attempts >= 5` - dead-letter
  inspection.
- No table change (the `007` queue table already has `claimed_at`, `done_at`,
  `attempts`, `last_error`, and the drain index). Wire into `000_run_all.sql`
  after `009`. Apply is **gated** to the cutover runbook (Section 9).

## 7. Observability

- `cre_status.sh` reports the new `enrich` tier (schedule, last-run verdict,
  staleness) alongside monitor/weekly.
- A one-line queue health probe (pending count, dead-letter count, oldest
  pending age) added to `cre_status.sh` via the two `010` views.
- Per-run marker `out/daily/last_run_enrich.json` (same shape as other tiers,
  written by `cre_run_tier.sh`).

## 8. Testing

- **pytest** (`tests/`): `cre_enrich.py` claim idempotency, stale-claim reclaim,
  dead-letter at attempts>=5, complete marks done only for present external_ids,
  retry leaves absent ones, and a guard test asserting the worker never emits
  `--mark-missing` / `--activate-status`. Reuse the existing psql-mock patterns
  in `tests/`.
- **TS unit**: `--enrich-input` grouping + artifact shape; generic fallback
  extraction on a fixture HTML; colliers-main enricher against a saved detail
  fixture.
- **End-to-end dry run**: seed a tiny synthetic queue (2-3 rows for
  colliers-main against real but capped URLs), run the worker, assert
  `cre_listing_price_history` / child rows update and the queue rows flip to
  `done_at`. Gated behind the live stack being healthy.
- `tsc --noEmit`, `py_compile`, and `bash -n` on every changed script.

## 9. Cutover runbook (GATED - requires owner go-ahead)

Non-destructive prep (safe, done by the workflow):
1. Land code + tests + docs on the feature branch; CI green.
2. Render + install new plists (does NOT load): `bash launchd/install_launchd.sh
   monitor enrich weekly`.

Live cutover (operator runs; the workflow only prints these unless invoked with
`{cutover:true}`):
3. Apply SQL: `psql "$DATABASE_URL" -f sql/010_cre_enrichment_ops.sql` (additive
   views only).
4. Reload monitor at the new 2x/day cadence: `launchctl unload` then `load -w`
   the monitor plist.
5. Load enrich: `bash launchd/install_launchd.sh --load enrich`.
6. Retire daily: `launchctl unload ~/Library/LaunchAgents/ai.agentic.cre-daily.plist`.
7. Verify: `bash cre_status.sh` shows monitor (2x/day), enrich (4h), weekly
   (held), daily gone; queue health probe non-erroring.

**Still held regardless of cutover** (unchanged from prior gating):
- weekly `--mark-missing` load (the only soft-delete tier),
- first live status activation,
- consumer board-gate deploy.

## 10. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Worker soft-deletes or flips status | Additive by construction: always `--no-mark-missing`, never `--activate-status`; review phase asserts this on the diff. |
| Detail change missed because enumeration can't see it | Weekly full scrape is the backstop; nothing relies solely on enrich. |
| Generic fallback captures little for SPA/iframe sources | Bespoke enrichers for the top-3 detail sources; others ride weekly until their enricher lands. |
| Queue starvation / poison rows | `attempts < 5` dead-letter + `v_cre_enrichment_dead`; oldest-pending-age in heartbeat. |
| Crashed worker leaves rows claimed | `claimed_at < now()-1h` reclaim. |
| Monitor 2x/day raises disappearance latency | Disappearances are observe-only until weekly mark-missing anyway; no board impact. |
| Concurrent tier overlap | Shared `mkdir` lock; enrich offset 30 min from monitor. |

## 11. Out of scope / follow-up

- Bespoke enrichers for the remaining ~12 sources (incremental; weekly backstop
  covers them meanwhile).
- Priority tuning (e.g. price_change > new) via `cre_enrichment_queue.priority`.
- Per-rescrape child versioning, image archival, M4 sub-daily detection (already
  deferred in the freshness review).
- Promoting enrich to event-driven (run-on-enqueue) instead of fixed 4h.
