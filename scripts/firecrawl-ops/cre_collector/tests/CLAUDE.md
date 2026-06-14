# tests Module

## Most Critical Rule

**Pure-transform, no-network tests only.** Import and call real functions from `cre_ingest.py`, `cre_gate.py`, and `cre_monitor.py`. No live DB, Supabase, or Firecrawl. Use synthetic JSON and temp files unless a file header says otherwise.

**Observe-only SQL invariant:** `build_write_sql` (`cre_monitor.py`) and `build_baseline_sql` (`cre_gate.py`) must never assign `cre_listings.status` or `cre_listings.deleted_at`. Tests grep generated SQL (non-comment lines) to enforce this. `cre_ingest.py` mark-missing SQL is out of scope here.

**Re-implementation rule:** Do not duplicate production logic in assertions. Exception: `test_enum_key_invariant.py` defines `monitor_enum_key()` as the explicit contract mirror for enumeration id vs `to_row().external_id`.

## Folder-Specific Commands

**Python (ingest, monitor, gate):**

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/ -q
python3 -m pytest tests/test_enum_key_invariant.py -q
python3 -m pytest tests/test_norm_status_canonical_and_guards.py -q   # portable CI signal (no out/)
```

Requires `pytest` on the host (not pinned in `package.json`). Full suite:
**261** pytest pass as of 2026-06-14 (`python3 -m pytest tests/ -q`); the count
includes parametrized and data-driven cases, so re-run to confirm rather than
counting `def test_`.

**TypeScript (collector helpers, adapters):**

```bash
cd scripts/firecrawl-ops/cre_collector
npm run test:unit
npm run typecheck
npm test   # typecheck + test:unit
```

Uses Node built-in `node:test` + existing `tsx` devDependency (no vitest/jest).
Add files as `tests/ts/*.test.ts` or `tests/ts/**/*.test.ts`. See `tests/ts/README.md`.

## Test Files

| File | Focus |
|------|-------|
| `test_cre_gate.py` | `cre_gate`: `verdict_for`, `rolling_median`, `select_baseline_updates`, `count_artifacts` |
| `test_gate.py` | Gate plus ingest monitor helpers: `norm_status` terminal-wins, `rolling_median`, `select_baseline_updates`, `build_baseline_sql` safety, `rollup_brokerages` |
| `test_monitor.py` | Broad `cre_monitor` coverage: `load_artifact_groups` (errored/truncated fifth-value fold), `coverage_decision` (0.7 boundary, errored not overridable by force), `finalize_group`, `compute_fingerprint`, `derive_events`, `build_write_sql` SQL safety |
| `test_monitor_events.py` | `derive_events` event types, baseline-seed suppression, idempotency, `build_write_sql` safety |
| `test_norm_status_shapes.py` | `norm_status` and `STATUS_SOURCE_PATHS` against gitignored `out/*.json` (skips when absent) |
| `test_norm_status_canonical_and_guards.py` | `_canonical_key`, word-boundary guards, source-classification completeness; no `out/` dependency |
| `test_enum_key_invariant.py` | Enumeration id equals ingest `external_id` for every `SOURCE_TO_BROKERAGE` key |
| `test_ingest_status_activation.py` | OPT-IN status activation: `_status_activation_enabled()`, `apply_status_activation_gate()`, default-off no-op, terminal-stickiness guard |

`conftest.py` prepends parent `cre_collector/` to `sys.path` (no package install).

## TypeScript unit tests (`tests/ts/`)

**Pure-transform, no-network tests only.** Import and call real functions from
`lib/`, `sources/`, or `types.ts`. No live Firecrawl, no `collect.ts` E2E runs.

**Import gotcha:** `lib/config.ts` runs `parseArgs()` at import time from
`process.argv`. Tests that need scrape/util helpers without CLI side effects
should import those modules directly, not `config.ts` or `collect.ts`.

**ESM:** use `.js` suffix on relative imports in test files (matches production).

| File | Focus |
|------|-------|
| `smoke.test.ts` | Harness wiring smoke check |
| `lib/util.test.ts` | `clean`, `num`, `boundedInt`, `moneyToNumber`, `isPerSfPriceText`, `prune`, `pmap` |
| `lib/html.test.ts` | `decodeHtmlEntities`, `jsonLdObjects`, `firstJsonLd`, sitemap XML, `dedupeStrings` |
| `lib/scrape.test.ts` | `parseJsonBody`, `repairUnescapedJsonStringQuotes` (no network) |
| `lib/broker.test.ts` | `brokerRef` dedupe and field merge (`resetBrokerStateForTests`) |
| `sources/transwestern.test.ts` | URL helpers, price/size text, facts/availability HTML parsers |
| `sources/buildout.test.ts` | inventory URL, env helpers, cache path/window |
| `sources/marcus-millichap.test.ts` | URL/location parsers, tile HTML mapping |
| `sources/cbre-dealflow.test.ts` | location, listing PV, engine key extraction |
| `sources/savills.test.ts` | ZIP state inference, location, sqft, image URLs |
| `sources/newmark.test.ts` | `normalizePersonName`, `newmarkState` |
| `sources/cbre.test.ts` | `cbreAspect`, `cbreListingSlug`, `cbreListingUrl`, `cbreBrochureUrl`, `cbrePhotoUrl`, `cbreTransactionType` |
| `sources/jll.test.ts` | search URL helpers, `__NEXT_DATA__`, contacts, `parseJllSearchPage`, detail cache round-trip |
| `sources/jll-investor.test.ts` | investor sitemap/search helpers, document/image URLs, contacts |
| `sources/colliers.test.ts` | RCM URLs, location, cards, detail contacts/images |
| `sources/colliers-main.test.ts` | challenge detection, sitemap, address/JSON-LD, detail cache JSONL |
| `sources/cushman-wakefield.test.ts` | URL canonicalization, asset dedupe, markdown/numeric parsers, contacts |
| `sources/avison-young.test.ts` | SharpLaunch CDN/URL, transaction classification, detail extraction |
| `sources/nai-global.test.ts` | Infabode location, price/size/status, `naiListingFromFeed` |

Full suite: ~**157** TypeScript unit tests (`npm run test:unit`); re-run to
confirm the exact count rather than trusting this figure.

**Argv isolation:** source adapters import `lib/config.ts`; trim `process.argv` to
`[node, script]` before those imports in test files (see `lib/scrape.test.ts`).

**Still E2E/probe only:** async `src*()` collectors and network-bound enrich/fetch
orchestrators (`scrapeJson`, `scrapeDoc`, `fetch`, Firecrawl). Golden-file adapter
output tests remain future work.

## Module Boundaries

Owns Python ingest, monitor, and gate unit contracts **and** TypeScript
collector unit contracts under `tests/ts/`. Does **not** own E2E `collect.ts`
runs, live `cre_ingest.py --apply`, or `cre_validate.py` / `npm run validate:supabase`.

Overlap across `test_gate.py`, `test_cre_gate.py`, and `test_monitor.py` is intentional regression coverage.

## Integration Points

- **`cre_ingest`**: `to_row`, `merge_rows`, `norm_status`, `_canonical_key`, `SOURCE_TO_BROKERAGE`, `STATUS_SOURCE_PATHS`
- **`cre_gate`**: `verdict_for`, `rolling_median`, `select_baseline_updates`, `build_baseline_sql`, `rollup_brokerages`, `count_artifacts`
- **`cre_monitor`**: `load_artifact_groups`, `coverage_decision`, `finalize_group`, `compute_fingerprint`, `derive_events`, `build_write_sql`
- Id prefix, status paths, gate floors, or observe-only SQL changes need matching test updates in the same PR.

## References

- `../cre_ingest.py`, `../cre_gate.py`, `../cre_monitor.py`
- `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
- `../../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` (especially sections 6, 8, 9, 12, 14.4)
