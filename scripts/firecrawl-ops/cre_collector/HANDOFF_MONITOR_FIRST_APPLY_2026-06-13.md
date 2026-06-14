# Handoff: Monitor hardening, collect.ts modular refactor, first gated --apply seed

Date: 2026-06-13 (UTC writes stamped 2026-06-14). Branch:
`feat/cre-brokerage-collectors-2026-06-12`. This records the monitor-layer
hardening, the `collect.ts` module split, and the first live `cre_monitor.py
--apply` seed. Pairs with `START_HERE.md` (live status) and
`docs/firecrawl-ops/references/cre-monitor-subsystem.md` (run model + gotchas).

## What shipped (committed `8d38e9cac` and the auto-commits before it)

Track 1, pre-`--apply` correctness and robustness for the observe-only monitor.
All verified: `npm run typecheck` clean, `python3 -m pytest tests/` 237 passing
(was 222), and two independent opus adversarial reviews (refactor equivalence,
monitor correctness).

1. **Monitor exclusions expanded to four.** `collect.ts --monitor` now returns
   `listings: []` for `jll`, `jll-investor`, `cbre-dealflow`, and `colliers`
   (SalesTracker). The two new ones were a confirmed enum-key invariant
   violation: their persisted `external_id` is detail-derived, so the cheap
   enumeration emits a different key than the ingest persisted, which would
   orphan the change ledger.
   - `cbre-dealflow`: monitor card id is the URL `listingPv` token; ingest
     persists `data.projectid`. ~1,430 / 1,836 (78%) mismatch.
   - `colliers` SalesTracker: monitor card id is the `GetMapData` ProjectId
     paired by array index (fragile); ingest persists the SLP-detail ProjectId.
     ~581 / 1,300 (45%) mismatch.
   - `colliers-main` (`main:` sitemap ids) keys on the same `entry.id` in both
     modes, so it stays monitor-enabled. An adversarial per-source audit
     confirmed no other source needs exclusion (cushman / newmark / marcus /
     avison / transwestern / nai-global all spread `...base` and never override
     the id; cbre keys on `Common.PrimaryKey`; svn / lee key on the URL
     `propertyId`).

2. **0-row monitor run no longer throws.** A `--monitor` run that enumerates
   only excluded sources writes an empty artifact (exit 0) instead of the
   top-level "no listings collected" hard error. Full mode still throws on 0.

3. **Coverage gate hardened (`cre_monitor.py`).** Extracted a pure
   `coverage_decision(...)` (unit-tested at the 0.7 boundary). Disappearance is
   now triple-gated: the 0.7 coverage fraction, `run_source_keys` membership,
   and a refusal for any source whose enumeration pass `errored` OR `truncated`
   this run. The error/truncated gate is NOT overridable by `--force-disappear`
   (forcing disappearance on a known-partial pass is the exact mass soft-delete
   hazard). Sources signal a partial pass via a new `SourceResult.truncated`
   flag; `load_artifact_groups` folds `sources[].error` and `sources[].truncated`
   into `errored_source_keys`.
   - Adapters that set `truncated`: `newmark` (Algolia ~1000-hit cap unsplit),
     `cbre` / `cushman-wakefield` (collected < `min(max, reported total)`),
     `nai-global` (PAGE_CAP clip with a full last page).
   - Residual (tracked): `colliers-main` sub-30% partial XML sitemap read is not
     cleanly detectable at the adapter; the 0.7 fraction still catches >30%
     drops. A thrown fetch already lands in `errored_source_keys`.

4. **`collect.ts` split into modules (behavior-preserving).** The 5,759-line
   monolith is now a 247-line CLI entry plus `types.ts`, `lib/` (config, scrape,
   util, broker, html), and `sources/<broker>.ts` (one adapter per brokerage).
   Pure mechanical move: declaration-name set identical (309), code-line multiset
   identical, only the old top banner comment dropped. `collect.ts` stays the CLI
   entry (`tsx collect.ts`), so `cre_daily_update.sh`, `run_colliers_main_full.sh`,
   and launchd are unaffected. NodeNext ESM: relative imports use `.js`
   extensions; `tsconfig.json` `include` widened to cover the new files. A
   network-free `--source=colliers,cbre-dealflow --monitor` run confirmed the
   entry + exclusions + empty-artifact path end to end.

5. Docs updated: `cre_collector/CLAUDE.md`, `sources/CLAUDE.md`, `lib/CLAUDE.md`,
   `launchd/CLAUDE.md`, `launchd/README.md` (de-staled), `cre_run_tier.sh`
   (monitor dispatch now runs `collect.ts --monitor` then `cre_monitor.py`,
   observe-only unless `CRE_MONITOR_APPLY=1`), and
   `docs/firecrawl-ops/references/cre-monitor-subsystem.md`.

Backups of the pre-refactor `collect.ts` and the pre-refactor declaration list
live under `tasks/tmp/cre-track1-baseline-2026-06-13/` (gitignored scratch).

## First gated `--apply` seed (DONE, verified)

Seeded one well-behaved, monitor-enabled source to prove the apply path against
prod with no risk. Source: `avison-young` (SharpLaunch feed, monitor=enum,
stable `row.id` that matches the persisted `external_id`).

```bash
cd scripts/firecrawl-ops/cre_collector
ART=out/monitor/seed_avison_2026-06-13_195457.json   # mode=monitor, 2332 listings, truncated=false
npx tsx collect.ts --source=avison-young --transaction=both --monitor --out="$ART"
python3 cre_gate.py    --in "$ART" --apply --update-baseline   # seeds cre_source_baseline
python3 cre_monitor.py --in "$ART" --apply                     # seeds cre_source_index (baseline seed)
```

Pre-write state was confirmed empty (all four 007 tables = 0 rows; board =
72,544 active). Post-seed verification (Supabase MCP, project
`fhqycqubkkrdgzswccwd`):

| Table / metric | Result |
|---|---|
| `cre_source_baseline` rows | 1 (avison-young) |
| `cre_source_index` rows (avison) | 2199 |
| `cre_listing_events` | 0 (baseline seed fires no events) |
| `cre_enrichment_queue` | 0 |
| board active total | 72,544 (unchanged) |
| avison soft-deleted by seed | 0 |
| avison `status` changes | 0 |

The monitor wrote only `cre_scrape_jobs` (run row), `cre_source_index`, and a
guarded neutral `cre_listings` UPDATE (`source_lastmod` / `canonical_key` only,
IS DISTINCT FROM). It never touched `status` or `deleted_at`. Dry-runs of both
tools were inspected before applying.

### One side effect, assessed and accepted

The neutral `canonical_key` backfill (NULL -> set on the 2199 avison rows) trips
the BEFORE UPDATE trigger `trg_cre_listings_updated_at` (`005_cre_views.sql:280`),
which stamps `updated_at = now()`. EQUIRE sorts the board by `updated_at desc`
(`dynamically-display-cre-listing-data/lib/db/credeals.ts`), so those rows moved
to the top of the recently-updated feed. This is within normal churn: the daily
ingest already sets `updated_at = now()` on every upserted row unconditionally
(`cre_ingest.py:821`), so `updated_at` reshuffles board-wide every daily run and
the seed bump self-levels on the next ingest. No status/visibility/price change.
Optional future nicety (not required): make `trg_cre_listings_updated_at`
content-aware so the monitor's internal-column writes do not bump `updated_at`,
making the monitor purely observe-only with respect to the board.

## Next steps (all gated for explicit go-ahead)

- **T2.3 - scale the seed to the other ~10 monitor-enabled sources.** Run
  `collect.ts --source=all --monitor` (about 20-30 min, like the daily full run)
  then `cre_gate.py --apply --update-baseline` and `cre_monitor.py --apply`.
  Excluded sources (`jll`, `jll-investor`, `cbre-dealflow`, `colliers`) emit 0
  monitor rows and stay on the full-sweep cadence. Each unseeded source is a
  silent baseline seed (no events).
- **T3.1 - Phase-2 status activation in `cre_ingest.py`.** Wire `norm_status`
  into the upsert via COALESCE (Choice a in
  `docs/firecrawl-ops/references/cre-phase2-board-impact-2026-06-13.md`): only
  ever upgrade `status` to a real terminal/under_contract/pending signal; no-signal
  rows stay `'active'` (status never NULL, no coverage cliff). Add `status` (+
  `source_lastmod` / `canonical_key`) to `STAGE_COLS` and the INSERT/UPDATE.
- **T3.2 - EQUIRE board-filter edit (second repo, live board).** In
  `dynamically-display-cre-listing-data`: `lib/listing-filters.ts` (~line 129)
  and `lib/db/credeals.ts` (~line 202 `BOARD_STATS_QUERY`), widen `'active'`-only
  to `status IN ('active','under_contract','pending')` (Option B). Feature branch
  only; never main; never merge without the literal phrase "Cayman approved this
  merge". Couple with T3.1.
- **Before launchd / unattended cadence:** load no plist until the first full
  `--apply` and the gate wiring are approved. Consider closing the colliers-main
  partial-XML truncation residual.

## Invariants to keep

- Monitor is observe-only: never writes `cre_listings.status` or `deleted_at`.
- Monitor enumeration key == ingest `external_id`. Any source whose persisted id
  is detail-derived must be excluded from `--monitor` (see the four above).
- Never feed a `--monitor` artifact to `cre_ingest.py`.
- The ingestor and monitor print only the env file path, never `POSTGRES_URL`.
