# Tier-B Enrichment Worker + Cadence Restructure (Design)

> Status: IMPLEMENTED (code shipped 2026-06-15; live launchd cutover still
> GATED, Section 9). Author date: 2026-06-15.
> Companion executable plan: `workflows/cre_enrichment_worker.workflow.js`.
> Owner sign-off required before the live launchd cutover (Section 9).
> Reviewed 2026-06-15 against the live code; corrections folded in (Section 12).
>
> What shipped (matches this design, no behavioral divergence): `cre_enrich.py`
> (pure builders + thin `run()`), `collect.ts --enrich-input` with
> `lib/enrich.ts` (colliers-main + jll-investor bespoke enrichers, generic
> JSON-LD fallback), `sql/010_cre_enrichment_ops.sql` (the two health views,
> wired into `000_run_all.sql` after `009`), the restructured launchd tier set
> (`cre_run_tier.sh` `enrich`/`weekly` cases + retired `daily` case;
> `ai.agentic.cre-enrich.plist.template`; monitor template at 06:10/18:10;
> `install_launchd.sh` TIERS = monitor/enrich/weekly), and `cre_status.sh`
> (monitor/enrich/weekly loop, 18h monitor + 6h enrich staleness, `out/enrich`
> artifact scan). The ONE divergence from steady state is operational, not in
> code: the live Mac still has the OLD tiers loaded (`ai.agentic.cre-monitor`
> every 3h + `ai.agentic.cre-daily` 06:30). The Section 9 cutover (apply `010`,
> reload monitor at 2x/day, load enrich, unload daily, load the additive weekly
> backstop) has NOT been run and remains gated for owner go-ahead.

## 1. Why

Today there are two collection paths (`collect.ts`):

- **Monitor path** (`--monitor`): cheap enumeration only (list page, internal
  JSON API, or sitemap), emits index-level fields, skips the detail-page render.
  Runs every 3h, diffs against `cre_source_index`, records
  new/changed/disappeared events, and **enqueues** new/changed listings into
  `cre_enrichment_queue`.
- **Full path** (no flag): enumeration **plus** a Firecrawl detail render per
  listing (price, sqft, cap rate, contacts, documents, images). Runs nightly
  (06:30) across all ~60k listings. This is the expensive job.

The gap: nothing drains `cre_enrichment_queue`. So the only thing that refreshes
*detail* is the nightly full re-scrape of everything. That is wasteful (it
re-renders ~60k pages to catch the few hundred that changed) and slow.

This design closes the loop the monitor already opened, and rebalances cadence:

- Build the **enrichment worker** that drains the queue and scrapes ONLY the
  listings the monitor flagged as new/changed.
- **Monitor** drops from every-3h to **twice daily**.
- The **full re-scrape** moves from **daily to weekly** and runs **additively**
  (no soft-delete). It becomes the detail-refresh + dead-letter-recovery safety
  net.
- The heavy **daily tier is retired**; monitor (2x/day) + enrich (every 4h)
  replace its freshness role at a fraction of the cost.

Target steady state: detail changes reach the board within hours (monitor
detection latency) instead of up to 24h, and per-day Firecrawl detail renders
drop from ~60k to the few hundred that actually changed.

## 2. Target tier model

| Tier | Schedule (local) | Action | Soft-delete? | Loaded? |
|------|------------------|--------|--------------|---------|
| **monitor** | 2x/day (06:10, 18:10) | `collect --monitor` then `cre_monitor.py --apply`: enumerate all, detect new/changed/disappeared, enqueue new/changed | No | Yes |
| **enrich** (NEW) | every 4h (00:30, 04:30, 08:30, 12:30, 16:30, 20:30) | `cre_enrich.py`: claim a batch from `cre_enrichment_queue`, run `collect --enrich-input` (targeted detail), `cre_ingest.py --in` (additive), then delete done / retry the rest | No | Yes |
| **weekly** | Sun 03:00 | `collect` (full) then `cre_gate.py --strict` then `cre_ingest.py --in` (**additive by default**); the detail + dead-letter backstop | No by default; `--mark-missing` only under the `CRE_WEEKLY_MARK_MISSING=1` escalation (Section 2.1) | Yes (additive is safe to load) |
| ~~daily~~ | retired | unloaded; case kept for rollback | n/a | No |

Enrich is offset 30 min from the top of the hour so it never collides with the
monitor (06:10/18:10). The shared `mkdir` lock (`out/daily/.cre.lock`) is the
backstop: if two tiers ever overlap, the holder wins and the competitor exits 0.

### 2.1 Soft-delete invariant (preserved, non-negotiable)

Soft-delete (`--mark-missing`) is produced by **exactly one tier (weekly)** and
**only when explicitly escalated** with `CRE_WEEKLY_MARK_MISSING=1` in the weekly
plist environment (or run by hand). By default the weekly tier runs additive
(`cre_daily_update.sh` with `--no-mark-missing`), so loading it is safe and adds
a real backstop without any deletion risk. Even when escalated, mark-missing
stays triple-gated:

1. **Dispatcher gate** (`cre_run_tier.sh`): only the `weekly` branch can pass
   `--mark-missing`, and only when `CRE_WEEKLY_MARK_MISSING=1`. `enrich` and the
   retired `daily` branch never pass it.
2. **Coverage gate** (`cre_daily_update.sh`): `cre_gate.py --strict` must return
   0; any source "hold" auto-downgrades that run to `--no-mark-missing`.
3. **Ingest eligibility** (`cre_ingest.py`): per-brokerage `errors==0 &&
   staged>=floor && complete_folded_coverage` before any soft-delete.

The enrich worker is additive **by construction**: it always calls
`cre_ingest.py --in` (additive is the default; `cre_ingest.py` has no
`--no-mark-missing` flag and never receives `--mark-missing`), never activates
status (status activation stays default-off), and feeds only the claimed
listings. It cannot soft-delete or flip board state. Moving the full re-scrape
from daily to weekly does NOT move soft-delete authority: the escalation is
still held for go-ahead.

## 3. Component 1 - `collect.ts --enrich-input` (targeted detail)

### Problem
`collect.ts` today has no per-listing mode: each source is a monolithic
`src<Name>(tx, max, monitor)` that enumerates then enriches as one loop
(`collect.ts:74` `runSource`, dispatched on the `monitor` boolean). The worker
needs to scrape an arbitrary set of specific listings.

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

`collect.ts` adds `--enrich-input` to the `parseArgs` options in `lib/config.ts`,
groups items by `sourceKey`, dispatches each group to a registered
**SourceEnricher** (generic fallback when none), and emits the standard artifact
(`{ runMeta:{mode:"enrich"}, sources, listings, brokers, totalListings }`) that
`cre_ingest.py` already consumes. No ingest change is required.

### Two id facts the enrichers MUST honor
1. **The queue carries the folded/prefixed external id, the artifact carries the
   native source id.** The monitor enqueues `external_id` equal to the *ingest*
   key (`cre_monitor.py` reuses `cre_ingest.to_row`), so it is prefixed
   (`main:`, `investor:`, `dealflow:`). But a source adapter emits the **native**
   id (`colliers-main.ts:233` sets `id: entry.id`), and `cre_ingest.to_row`
   re-applies the prefix on ingest. So each enricher must **strip its
   `SOURCE_TO_BROKERAGE` prefix** off `EnrichItem.externalId` to rebuild the
   native id before parsing; otherwise re-ingest double-prefixes
   (`main:main:...`), never matches the queued row, and the row dead-letters.
2. **Completion is matched by URL, not external id.** Every enricher must echo
   `EnrichItem.url` onto its output `listing.url` (the colliers reference already
   does: `parseColliersMainDetail` sets `url: entry.url`). The worker then marks
   a claimed row done iff its `url` appears among the emitted listings' urls. URL
   is verbatim in both the queue (`cre_monitor.py` stores `g["url"]`) and the
   artifact, so the join is exact and needs no prefix logic.

### SourceEnricher interface (new, in `lib/enrich.ts`)
```ts
export type EnrichItem = { sourceKey: string; externalId: string; url: string;
                           transaction?: "sale" | "lease" };
export interface SourceEnricher {
  // Scrape + parse the given listings' detail pages into standard listing rows.
  // Each returned row MUST carry listing.url === the input EnrichItem.url.
  // Omit a row on unrecoverable per-item failure (worker leaves it queued).
  enrich(items: EnrichItem[]): Promise<any[]>;
}
export const ENRICHERS: Partial<Record<SourceKey, SourceEnricher>> = { ... };
```

### Per-source enrichers (phased)
- **Phase 1 (this work): the two confirmed per-listing detail sources.**
  - `colliers-main`: already factored. Reuse exported
    `scrapeColliersMainDetailDoc(url)` + `parseColliersMainDetail(entry, doc)`
    (`sources/colliers-main.ts:62/215`), building a minimal
    `ColliersMainEntry = { url, lastmod: null, id: <native id> }` from the
    stripped external id. This is the reference implementation.
  - `jll-investor`: detail render then extract from `__NEXT_DATA__`
    (`jll-investor.ts:38` `jllInvestorNextData`, used at `:198-251`). Factor the
    inline detail parse into an exported `parseJllInvestorDetail` and reuse it.
- **CBRE is excluded on purpose.** `cbre` is **enumeration-only**
  (`cbre.ts:51`: the listings-api JSON already returns fully mapped rows with no
  per-listing detail render, so monitor output equals full output). There is no
  per-listing CBRE detail endpoint to enrich, so CBRE has no bespoke enricher;
  its new/changed rows ride the weekly additive backstop until a future
  query-replay enricher is added (Section 11).
- **Generic fallback enricher** (`lib/enrich.ts`): for any source without a
  bespoke enricher, scrape the URL via `scrapeDoc` (`lib/scrape.ts`) and run the
  shared JSON-LD extractors `jsonLdObjects` / `firstJsonLd` (`lib/html.ts`).
  Best-effort: captures price/status/description where the page exposes JSON-LD.
  Logged as `enricher=generic` so coverage is visible. It still echoes the input
  url.
- **Sources with neither bespoke nor useful generic extraction**: the enricher
  returns `[]` for those items; the worker leaves them queued and they are
  refreshed by the **weekly full scrape** (safe degradation, logged).

No listing is ever lost: anything the worker cannot enrich is simply refreshed
weekly, exactly as today.

## 4. Component 2 - `cre_enrich.py` (queue worker / orchestrator)

New script mirroring `cre_monitor.py` / `cre_ingest.py` conventions (argparse,
`cre_ingest.load_db_url` for env-file precedence, psql shell-out, never prints
the URL). For testability it is split into **pure builders** plus a thin `run()`
(Section 8 asserts on the builders with no DB):

- `build_claim_sql(batch, *, reclaim_interval="1 hour") -> str`
- `build_collect_argv(claim_path, out_path) -> list[str]`
- `build_ingest_argv(enriched_path) -> list[str]`  (always `["--in", path]`)
- `select_done_and_retry(claimed_rows, enriched_listings) -> (done_ids, retry_ids, dead_ids)`  (URL match, id-keyed completion)
- `build_complete_sql(done_ids) -> str`  and  `build_release_sql(claimed_ids) -> str`

### Flow
1. **Claim** a batch (default 200) in one atomic statement:
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
     SET claimed_at = now()
     FROM claimed WHERE q.id = claimed.id
   RETURNING q.id, q.source_key, q.external_id, q.url, q.reason;
   ```
   - The row locks taken by `FOR UPDATE SKIP LOCKED` in the CTE are held across
     the outer `UPDATE ... FROM claimed` (single statement), so the claim is
     race-safe even though the `mkdir` lock already serializes tiers.
   - `claimed_at < now() - 1h` reclaims rows from a crashed prior run.
   - `attempts < 5` skips dead-lettered rows.
   - **`attempts` is NOT incremented here.** (See step 6: incrementing at claim
     time would let a single stack-down run burn an attempt for the whole batch,
     dead-lettering healthy rows after five systemic outages.)
2. If zero claimed: exit 0 (cheap no-op). No subprocess runs.
3. Write claimed rows to `out/enrich/claim_<stamp>.json`.
4. `npx tsx collect.ts --enrich-input=claim.json --out=enriched.json`.
5. **Gate on collect success before ingesting.** If the collect subprocess exits
   nonzero, or `enriched.json` is missing / invalid JSON / has zero listings,
   treat it as a whole-run failure: run `build_release_sql(claimed_ids)`
   (`SET claimed_at = NULL`, attempts untouched) so the batch retries free next
   run, set `last_error`, and exit nonzero (the dispatcher records the failure in
   the verdict marker). Do NOT ingest a partial/empty artifact.
6. On a successful collect: `python3 cre_ingest.py --in enriched.json` (additive;
   status activation OFF by default, so the `CRE_STATUS_FLIP_MAX_FRACTION`
   breaker is inert and a small batch cannot trip it). Then **complete** by
   claimed queue id, after deciding completion by URL:
   - `select_done_and_retry` splits the claimed rows: `done` = rows whose `url`
     is in the enriched artifact; `retry` = the rest. It returns the claimed
     queue ids for done rows so duplicate URL work with a different `reason`
     cannot be deleted accidentally.
   - `build_complete_sql(done_ids)` **DELETEs** the done rows by `id` (the queue
     is an ephemeral work queue; `cre_listing_events` is the durable audit).
     Deleting done rows is what lets a *later* change to the same listing re-enqueue:
     keeping them would let the monitor's `ON CONFLICT (brokerage_id,
     external_id, reason) DO NOTHING` suppress every future change.
   - For `retry` rows, increment `attempts` (only this claimed-but-absent set,
     only after a successful collect). When `attempts` reaches 5 the row leaves
     the drain set (`attempts < 5`) and surfaces in `v_cre_enrichment_dead`; set
     `last_error`.

### Safety properties
- Always `--in` only; cannot soft-delete, and never passes `--mark-missing` /
  `--no-mark-missing` (the latter is not a `cre_ingest.py` flag) / `--activate-status`.
- Partial-artifact safe: `cre_ingest.py` upsert is keyed on
  `(brokerage_id, external_id)`; L1 COALESCE-keep means a transient parse miss
  never blanks a prior good price; children are replaced only when there is no
  `detailError`.
- Idempotent and at-most-wasteful: if the worker crashes after ingest succeeds
  but before the DELETE, the next run re-claims those urls and re-enriches them.
  Re-ingest is idempotent (upsert key + COALESCE-keep), so the only cost is one
  redundant render, never corruption.
- psql safety: claim/complete/release SQL is built with `sql_lit` quote-doubling
  under `SET LOCAL standard_conforming_strings = on` and
  `-v ON_ERROR_STOP=1`, exactly like `cre_monitor.build_write_sql`. URL values
  (scraped text) are never f-string-interpolated; the DB url is never printed.
- Marker ownership: the worker communicates only via exit code (0 = success or
  empty-queue no-op; nonzero = collect/ingest failure). `cre_run_tier.sh` owns
  the `last_run_enrich.json` verdict marker, consistent with the other tiers.

## 5. Component 3 - cadence restructure (launchd)

Files to change:
- `launchd/ai.agentic.cre-monitor.plist.template` - replace the 8-entry
  `StartCalendarInterval` array with two entries (06:10, 18:10).
- `launchd/ai.agentic.cre-enrich.plist.template` (NEW) - 6-entry array
  (00:30/04:30/08:30/12:30/16:30/20:30); `EnvironmentVariables` = `PATH` +
  `__ENV_EXTRA__` (CRE_ENV_FILE); optional `CRE_ENRICH_BATCH`. No
  `CRE_MONITOR_APPLY`, no status-flip var.
- `launchd/install_launchd.sh` - add `enrich` to the TIERS list, `label_for`,
  and rendering; keep the `--load` gating.
- `launchd/cre_run_tier.sh` - add an `enrich)` case: `python3 cre_enrich.py
  --batch "${CRE_ENRICH_BATCH:-200}"`. Change the `weekly)` case so the
  `--mark-missing` flag is conditional:
  `MM="--no-mark-missing"; [ "${CRE_WEEKLY_MARK_MISSING:-0}" = "1" ] && MM="--mark-missing"`,
  then `bash cre_daily_update.sh "$MM"`. The `daily)` case is retired (left for
  rollback, no longer scheduled).
- `cre_status.sh` - replace the three `for tier in monitor daily weekly` loops
  (`:103/130/288`) with `monitor enrich weekly`; add `enrich) echo $(( 6*3600 ))`
  (1.5x the 4h cadence) to `stale_threshold` and change `monitor` from 4.5h to
  18h (1.5x the new 12h cadence); add `out/enrich` to `newest_artifact`.
- Retire daily at cutover: `launchctl unload` its plist (Section 9); keep
  `ai.agentic.cre-daily.plist.template` for rollback.

The weekly template stays; the only behavior change is that its dispatch is
additive unless `CRE_WEEKLY_MARK_MISSING=1` is set.

## 6. Component 4 - SQL (migration `010`, additive only)

`sql/010_cre_enrichment_ops.sql` (new), idempotent, `credeals` schema:
- `v_cre_enrichment_queue_pending` = `done_at IS NULL AND attempts < 5`
  (priority, enqueued_at order) - live work. (With delete-on-done, done rows are
  removed, so this is every live non-dead row.)
- `v_cre_enrichment_dead` = `done_at IS NULL AND attempts >= 5` - dead-letter
  inspection.
- No table change (the `007` queue already has `claimed_at`, `done_at`,
  `attempts`, `last_error`, and the drain index). Wire into `000_run_all.sql`
  after `009`. Apply is **gated** to the cutover runbook (Section 9).

## 7. Observability

- `cre_status.sh` reports the new `enrich` tier (schedule, last-run verdict,
  staleness) alongside monitor/weekly.
- A one-line queue health probe (pending count, dead-letter count, oldest
  pending age) added to `cre_status.sh` via the two `010` views.
- Per-run marker `out/daily/last_run_enrich.json` (same shape as the other
  tiers, written by `cre_run_tier.sh`, not by the worker).

## 8. Testing

All `cre_enrich.py` unit tests are pure-transform (assert on builder strings, the
`select_done_and_retry` partition, or argv lists) or `monkeypatch`
`subprocess.run` / `load_db_url`; none connect to a DB, per `tests/CLAUDE.md`.

- **pytest** (`tests/test_cre_enrich.py`):
  1. claim SQL contains `FOR UPDATE SKIP LOCKED`, `attempts < 5`, `done_at IS NULL`, `ORDER BY priority, enqueued_at`, `LIMIT`, and `RETURNING` with `url`; no `attempts + 1` at claim time.
  2. claim SQL reclaims stale claims (`claimed_at IS NULL OR claimed_at < now() - interval '1 hour'`).
  3. claim SQL is idempotent across calls; carries the `standard_conforming_strings` + `ON_ERROR_STOP` pins; never contains the DB url.
  4. `select_done_and_retry` marks done only urls present in the artifact, retries the rest.
  5. URL match works when the claimed `external_id` is folded (`main:usa1`) but the artifact carries native `id=usa1` with the same `url` (proves URL, not external-id, matching).
  6. `build_complete_sql` emits `DELETE ... WHERE id IN (...)` (sql_lit-quoted
     uuid values) for done rows and never a `done_at = now()` update; dead-letter
     rows are never deleted.
  7. a claimed-but-absent row at `attempts==4` partitions into the dead set (next claim's `attempts < 5` excludes it).
  8. a whole-run collect failure releases claims (`claimed_at = NULL`) without incrementing attempts, ingest is not invoked.
  9. an empty / missing / invalid `enriched.json` marks nothing done and skips ingest.
  10. ingest argv is exactly `["--in", path]`; never `--mark-missing` / `--no-mark-missing` / `--activate-status` (the key safety guard).
  11. collect argv targets `--enrich-input` + `--out`, no full/monitor flags.
  12. empty claim exits 0 and runs no subprocess.
  13. the DB url is never printed (only the env-file path may appear).
  14. env discovery reuses `cre_ingest.load_db_url` precedence (mirror `test_env_discovery.py`).
  15. the Phase-1 enricher set is exactly `{colliers-main, jll-investor}` and excludes `cbre` (C3).
- **TS unit** (`node:test`, run via `npm run test:unit`): `--enrich-input`
  grouping + artifact shape (`runMeta.mode==="enrich"`, every listing echoes its
  input url); the generic fallback extraction on a fixture HTML; the colliers
  enricher reconstructing a native id from a folded external id against a saved
  detail fixture.
- **End-to-end dry run** (gated on the local stack being healthy): seed 2-3
  synthetic colliers-main rows in a scratch queue artifact (do not mutate the
  prod queue), run `collect.ts --enrich-input` then `cre_ingest.py --dry-run`,
  and assert the artifact shape plus that no mark-missing / status path fires. If
  the stack is down, skip and say so.
- `npm run typecheck` (`tsc --noEmit`), `py_compile`, and `bash -n` on every
  changed script.

Current green baseline before any change: 344 pytest, 169 TS unit, `py_compile`
clean (2026-06-15).

## 9. Cutover runbook (GATED - requires owner go-ahead)

Non-destructive prep (safe, done by the workflow on a plain run):
1. Land code + tests + docs on the feature branch; CI green.
2. Render + install plists (does NOT load): `bash launchd/install_launchd.sh
   monitor enrich weekly`.

Live cutover (operator runs; the workflow only prints these unless invoked with
`{cutover:true}`, and even then it does not load weekly or enable mark-missing):
3. Apply SQL: `psql "$DATABASE_URL" -f sql/010_cre_enrichment_ops.sql` (additive
   views only).
4. Reload monitor at the new 2x/day cadence: `launchctl unload` then `load -w`
   the monitor plist.
5. Load enrich: `bash launchd/install_launchd.sh --load enrich`.
6. Retire daily: `launchctl unload ~/Library/LaunchAgents/ai.agentic.cre-daily.plist`.
7. Load the **additive** weekly backstop (operator decision; it is safe because
   `CRE_WEEKLY_MARK_MISSING` is unset, so it runs `--no-mark-missing`):
   `bash launchd/install_launchd.sh --load weekly`.
8. Verify: `bash cre_status.sh` shows monitor (2x/day), enrich (4h), weekly
   (additive), daily gone; queue health probe non-erroring.

**Still held regardless of cutover** (unchanged from prior gating):
- the `CRE_WEEKLY_MARK_MISSING=1` soft-delete escalation,
- first live status activation,
- consumer board-gate deploy.

## 10. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Worker soft-deletes or flips status | Additive by construction: only `--in`, never `--mark-missing` / `--activate-status`; review phase asserts this on the diff. |
| Re-ingest double-prefixes the external id | Enrichers strip the `SOURCE_TO_BROKERAGE` fold-prefix to rebuild the native id; covered by a TS round-trip test. |
| A second change to an enriched listing is suppressed | Delete-on-done keeps the queue ephemeral so the monitor re-enqueues the next change; `cre_listing_events` is the durable audit. |
| A stack outage dead-letters healthy rows | attempts is incremented only on claimed-but-absent rows after a successful collect; a whole-run failure releases claims untouched. |
| Detail change missed because enumeration can't see it | Weekly additive full scrape is the backstop; nothing relies solely on enrich. |
| Generic fallback captures little for SPA/iframe sources | Bespoke enrichers for the confirmed detail sources; others ride weekly. |
| Crashed worker leaves rows claimed | `claimed_at < now()-1h` reclaim. |
| Concurrent tier overlap | Shared `mkdir` lock; enrich offset 30 min from monitor. |

## 11. Out of scope / follow-up

- Bespoke enrichers for the remaining sources (incremental; weekly backstop
  covers them meanwhile). A CBRE query-replay enricher (re-run the listings-api
  query, pick changed external_ids) is a candidate once per-listing volume
  justifies it.
- Priority tuning (e.g. price_change > new) via `cre_enrichment_queue.priority`.
- Promoting enrich to event-driven (run-on-enqueue) instead of fixed 4h.
- The `CRE_WEEKLY_MARK_MISSING` escalation, status activation, and the consumer
  board-gate deploy stay gated for explicit go-ahead.

## 12. Review corrections folded in (2026-06-15)

Findings from the design review against live code (kept here so the divergence
from v1 is auditable; v1 archived at
`archive/ENRICHMENT_WORKER_DESIGN_2026-06-15.v1-prereview.md`):

- C1: `cre_ingest.py` has no `--no-mark-missing` flag; additive is the default.
  Every worker ingest call is `--in` only.
- C2/B1: the queue external id is folded/prefixed, the artifact id is native;
  completion is decided by URL, deletion is scoped to the claimed queue id, and
  enrichers strip the fold-prefix.
- C3: CBRE is enumeration-only; dropped from the Phase-1 bespoke set.
- C4: the worker deletes done rows so re-changes re-enqueue.
- B2/B3: attempts are incremented only on claimed-but-absent rows after a
  successful collect; whole-run failures release claims and exit nonzero.
- B5: claim/complete SQL uses `sql_lit` + GUC pins, never f-string URL or id
  interpolation.
- B8: the dispatcher owns the verdict marker; the worker uses exit codes.
- Cadence decision (owner, 2026-06-15): weekly runs additive and is loadable as
  the backstop; `--mark-missing` is a separate gated escalation.
