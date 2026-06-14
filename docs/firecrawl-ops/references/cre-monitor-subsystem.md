# CRE Monitor Subsystem (change-tracking layer)

Status as of 2026-06-13: built and adversarially reviewed
(`approve_for_gated_live_use`). Schema applied to prod. The first live `--apply`
run, the launchd schedule, and the gate wiring into the daily script are still
gated for explicit go-ahead. Phase-2 status activation is a separate path (see
`cre-phase2-board-impact-2026-06-13.md`).

This is the additive change-tracking layer that sits on top of the existing
collector + ingestor. It detects new / changed / disappeared listings cheaply,
maintains an append-only event ledger, and queues detail-enrichment work,
WITHOUT changing what EQUIRE shows. It is observe-only for `cre_listings`.

Canonical architecture is section 14 (and 6-9, 12) of
`cre-intelligence-system-design.md`. This file is the operational summary plus
the hard gotchas a new session needs before running anything.

## Components

- `sql/007_cre_change_tracking.sql` (APPLIED to prod `fhqycqubkkrdgzswccwd`,
  verified live 2026-06-13). Four additive tables, all under `credeals`:
  - `cre_listing_events`  -  append-only change ledger (new / price_change /
    status_change / reappeared / disappeared). Idempotent per
    `(listing_id, event_type, field, new_value, scrape_job_id)`.
  - `cre_source_index`  -  enumeration snapshot keyed by
    `(brokerage_id, external_id)`; carries `fingerprint`, `soft_deleted`,
    `observed_status`, `last_enumerated_at`.
  - `cre_enrichment_queue`  -  Tier-B detail-render work queue for new / changed
    listings.
  - `cre_source_baseline`  -  rolling-median coverage health, one row per
    source_key (the disappearance gate reads this).
- `collect.ts --monitor`  -  cheap-enumeration pass. Runs each source's
  list / search / API / sitemap step and emits the freely-available enumeration
  fields, skipping the per-listing detail render / enrichment. Additive and
  gated entirely on the flag; the default (non-monitor) path is byte-identical
  when the flag is absent. `runMeta.mode` is `"monitor"` or `"full"`.
- `cre_monitor.py`  -  OBSERVE-ONLY diff / event / snapshot runner. Reads the
  same artifact JSON `cre_ingest` reads, diffs against `cre_source_index` +
  `cre_listings`, writes `cre_listing_events`, refreshes `cre_source_index`,
  enqueues `cre_enrichment_queue`, and updates ONLY the neutral `cre_listings`
  columns (`source_lastmod`, `canonical_key`), change-guarded so it never churns
  `updated_at`. It NEVER writes `status` or `deleted_at`. `--dry-run` (the
  default) never connects; `--apply` writes one `ON_ERROR_STOP` transaction.
- `cre_gate.py`  -  per-source coverage-and-anomaly gate. Counts
  `to_row`-accepted listings per source, compares to the rolling baseline in
  `cre_source_baseline`, and emits `ok` / `hold` / `first_seen` plus a
  per-brokerage `mark_missing_safe` rollup. Observe-only for listings; the only
  table it can write is `cre_source_baseline`, under `--apply --update-baseline`.

## Hard gotchas (read before running)

1. **Never run `cre_ingest.py` on a monitor artifact, and never with
   `--mark-missing`.** A monitor artifact is sparse by design (no price for
   cushman / cbre-dealflow / nai-global / colliers-main / jll-investor, no
   contacts / documents / images, no `detailError`). The ingest upsert sets
   price columns and `raw_data` to EXCLUDED directly and wholesale
   DELETE+reinserts child rows for any touched listing, so pushing a monitor
   artifact through it would ERASE enriched prices, `raw_data`, and all child
   rows. Monitor artifacts go through `cre_monitor.py` ONLY. "Same JSON shape"
   means shape-compatible for the diff layer, not safe for the listings upsert.

2. **`jll`, `jll-investor`, `cbre-dealflow`, and `colliers` (SalesTracker) are excluded from monitor mode** (they return zero
   monitor listings and stay on the full-sweep cadence). Their persisted
   `external_id` is detail-derived and unrecoverable from cheap enumeration:
   jll uses the numeric `property.id` from `__NEXT_DATA__`; jll-investor uses
   the Salesforce `listing.id`; `cbre-dealflow` ingest persists `data.projectid`
   while the monitor card yields the URL `listingPv` token (~78% mismatch across
   ~1,836 rows); `colliers` (SalesTracker) ingest persists the SLP-detail
   `ProjectId` while the monitor card yields a `GetMapData` `ProjectId` paired
   by array index (~45% mismatch across ~1,300 rows). `colliers-main`
   (XML-sitemap ids) is unaffected and stays monitor-enabled. Verified
   11,230/11,230 (jll) and 934/934 (jll-investor) slug-vs-persisted-id mismatch
   against full artifacts. Emitting mismatched keys would make those rows read as
   NEW each run and flood `cre_listing_events` / `cre_enrichment_queue`. A cheap
   path would require URL-keyed reconciliation inside `cre_monitor.py` (not built).

3. **The enumeration-key invariant is load-bearing.** monitor key ==
   ingest `external_id`, because both reuse `cre_ingest.to_row`. Any source
   whose persisted `external_id` is detail-derived cannot be monitored cheaply
   and must be excluded (see gotcha 2). When adding monitor support to a source,
   confirm the cheap enumeration produces the same `id` the full path persists.

4. **A source emitting 0 monitor rows is safe.** `cre_monitor` derives
   `run_source_keys` from the finalized records, so a source with 0 listings is
   absent from `run_source_keys` and its prior rows are never evaluated for
   disappearance (loop guard: `if sk not in run_source_keys: continue`). This
   holds even under `--force-disappear`. That is why excluding jll / jll-investor
   is safe and not a mass-disappearance risk.

5. **Disappearance is double-gated (triple-gated with the error block).** A
   `disappeared` event fires only if the source is in `run_source_keys` AND it
   re-enumerated at least `DISAPPEAR_COVERAGE_FRACTION` (0.7) of its prior live
   index population. `--force-disappear` bypasses the coverage fraction but NOT
   `run_source_keys` membership. Additionally, the coverage gate refuses
   disappearance for any source whose enumeration pass reported an error this
   run; that error gate is NOT overridable by `--force-disappear`.

6. **Monitor emits supersets for `nai-global` and `colliers-main`.** Monitor
   skips detail-dependent filters (NAI `FOR_SALE_ON_MARKET`, colliers-main
   Sale/Lease classification), so it emits MORE rows than the full path. Correct
   for diffing (new ids then render, which applies the filter), but monitor row
   counts will not equal full counts and these rows must never be upserted as
   listings as-is.

7. **`colliers-main` monitor emits only on the sale pass** (lease pass returns
   empty) to avoid duplicating each sitemap URL across both transactionMode
   passes. Run monitor with `--transaction=both` (or `sale`); a `lease`-only
   monitor run enumerates zero colliers-main rows.

8. **`marcus-millichap` monitor keeps the lightweight `mappropertydetail`
   POST** (it is the only source of the DealId `external_id` and the
   `to_row`-required `PropertyUrl`); it skips only the heavy per-listing
   detail-HTML enrichment. The bulk `mapproperties` row alone carries neither.

9. **Enumeration-only sources get no `collect.ts` speedup in monitor mode.**
   `cbre`, `savills`, `svn`, `lee-associates` have no per-listing detail render
   to skip, so monitor request cost equals the full run (you still page the
   whole API / inventory / list to see ids). Their monitor benefit is purely
   downstream (fewer DB writes via the diff runner), not a faster collect.

## Run model (intended cadence, gated until go-ahead)

```bash
# 1) cheap enumeration of every source, both transactions
npx tsx collect.ts --source=all --transaction=both --max-items=0 \
  --page-cap=400 --monitor --out=out/monitor_run.json

# 2) dry-run the diff (no DB connection) to inspect per-source deltas
python3 cre_monitor.py --in out/monitor_run.json

# 3) coverage gate (live read; no listing writes)
python3 cre_gate.py --in out/monitor_run.json --apply

# 4) apply the diff: writes events / index / queue + neutral columns only
python3 cre_monitor.py --in out/monitor_run.json --apply
```

`--apply`, the launchd schedule, the `cre_gate` wiring into
`cre_daily_update.sh`, and Phase-2 status activation are all gated for explicit
go-ahead. The full-detail daily collect + `cre_ingest` path is unchanged and
remains the source of truth for listing content.

## Tests

`tests/` (run `python3 -m pytest tests/ -q` from `cre_collector/`) covers the
monitor and gate, including the neutral-update set clause, the change-guard
(`IS DISTINCT FROM`), REAPPEARED idempotency, the date-range parse guard, and
the gate's folded-coverage rollup.
