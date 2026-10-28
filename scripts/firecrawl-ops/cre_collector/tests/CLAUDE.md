# tests Module

## Most Critical Rule

**No live providers, production DB, Supabase or Firecrawl calls.** Prefer pure
transforms with real production functions and synthetic JSON/temp files. Owned
loopback-server fixtures and the explicit disposable PostgreSQL contracts below
are separate supported test boundaries.

**Observe-only SQL invariant:** `build_write_sql` (`cre_monitor.py`) and `build_baseline_sql` (`cre_gate.py`) must never assign `cre_listings.status` or `cre_listings.deleted_at`. Tests grep generated SQL (non-comment lines) to enforce this. `cre_ingest.py` mark-missing SQL is out of scope here.

**Re-implementation rule:** Do not duplicate production logic in assertions. Exception: `test_enum_key_invariant.py` defines `monitor_enum_key()` as the explicit contract mirror for enumeration id vs `to_row().external_id`.

For offline collector subprocesses, prove that the injected transport is used
before importing or invoking the real source. Where an unsupported test hook
could fall back to live transport, install a deny-network preload before imports.
Keep intentional owned-loopback fixtures outside blanket socket denial.

For Linux/WSL full-suite checks alongside a live runtime, an available
unprivileged network namespace can contain TCP/UDP access while allowing the
suite's own loopback servers. Verify namespace support first; from the genuine
standalone QA clone, the command carrier is:

```bash
unshare --user --map-current-user --net --keep-caps -- sh -c 'ip link set lo up && exec "$@"' qa <test-command> <arguments>
```

Only the new namespace's loopback is enabled; host services and public routes
are absent. This does not isolate files or filesystem Unix sockets, and does
not replace fake transports, database boundaries or the standalone-clone rule.
Preserve the real non-root UID: `--map-root-user`/`-r` makes capacity-approval
tests correctly refuse the apparent root account. Verify UID/EUID and an owned
loopback bind before the suite. Install the sidecar's pinned Node dependencies
as described in SETUP; Python's C10 loopback fixtures use its `tsx` package.
Keep normal configuration defaults unless the test deliberately varies them;
an unrelated URL override or rewritten interpreter argv can break the harness.

For website or provider variations, build fixtures from sanitized, genuinely
observed responses and call the real mapper and verifier. Cover supported
layout/value variants plus negative mutations for missing, malformed,
conflicting, and unknown critical fields. Assert that each mutation actually
changes its intended node, field or input fingerprint; writing to an absent
selector is not a negative test. Assert that unsupported identity or
required-field changes fail closed; do not make a positional-selector fixture
or a duplicated test parser the acceptance oracle. Run collector suites in a
standalone `git clone --no-hardlinks` as required by
[SETUP's isolated verification recipe](../SETUP.md#isolated-verification). See
the [brokerage completion playbook](../../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md).

Before another identity-parser production trial, use the playbook's
[full available corpus diagnosis](../../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md#diagnose-the-full-available-corpus-before-retrying).
Include accepted controls and alternate routes; distinguish route outcomes from
per-listing acceptance and historical replay from fresh coverage. Fixing one
comparison predicate does not prove the complete validator accepts the page.

## Folder-Specific Commands

Synthetic observation-store fixtures and their disposable PostgreSQL runners
are documented in [observation_store/README.md](observation_store/README.md).
They do not authorize real source data, production DDL, or database writes.

**Python (ingest, monitor, gate):**

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/ -q --maxfail=1
python3 -m pytest tests/test_enum_key_invariant.py -q
python3 -m pytest tests/test_norm_status_canonical_and_guards.py -q   # portable CI signal (no out/)
bash tests/run_om_facts_postgres_contract.sh  # opt-in disposable PostgreSQL 17 contract
bash tests/run_listing_lifecycle_postgres_contract.sh  # opt-in local PostgreSQL 14+ contract
bash tests/run_cre_validate_timing_postgres_contract.sh  # opt-in native psql timing contract
bash tests/run_ingest_institutional_postgres_contract.sh  # opt-in disposable institutional UPDATE contract
bash tests/run_cre_quality_postgres_contract.sh  # opt-in active/historical scope + bounded financial fallback contract
python3 tests/run_daum_subset_postgres_contract.py "$(mktemp -d)/proof"  # opt-in WSL/Linux DAUM subset transaction contract
python3 tests/run_jll_image_repair_postgres_contract.py  # opt-in exact-reference repair SQL contract
python3 tests/run_transwestern_image_transition_postgres_contract.py \
  --proof-dir "$(mktemp -d)/proof" --execute-disposable-fixture  # opt-in Transwestern image transition contract
```

Requires `pytest` on the host (not pinned in `package.json`). The suite includes
the finite restoration suites `test_cre_code_handoff_restoration.py`,
`test_cre_code_restoration_execution.py` and `test_cre_code_restoration_successor.py`.
They cover owned cleanup/release faults, packet-bound missed-run approval,
bounded native history and exact active successor joins using synthetic/private
temporary evidence. Native helper changes also require fresh PowerShell 7/5.1
capture proof. No offline result authorizes restoring a live task.
The suite includes
parametrized and data-driven cases (the shell-syntax guard is parametrized over
every `*.sh` in the collector), so totals depend on the candidate and inputs.
When reporting a run, include its exact command and output, candidate SHA,
code/input/dependency bindings, and scope; do not infer totals from this file
or count `def test_`. Reuse prior results only while those bindings, scope, and
freshness still apply. Rerun when changes, failures, or unresolved risks invalidate
prior proof, and run all applicable repository-required checks on the integrated
final candidate. Never present an older result as a new candidate pass.

The PostgreSQL contract runner is intentionally outside pytest: it applies the
source OM-facts migration and executes the generated production upsert in an
unexposed disposable PostgreSQL 17 container. It proves fresh-schema
five-column-key creation, refusal of legacy alignment without its psql approval
variable, and approved legacy idempotence. It requires Docker and always removes
its container; it must never target Supabase or a collector database.
The lifecycle contract runner creates and drops a uniquely named local test
database, proves migration/event/locking invariants, and exits 78 when the local
server is older than PostgreSQL 14. It never accepts a database URL.
The validation timing contract uses the native psql client against a
loopback-only disposable PostgreSQL container. It proves subsecond and
greater-than-one-second timing grammar, isolated result/timing channels, and
preservation of result rows that resemble timing output. It never accepts an
external database URL and never pulls a missing image.
The institutional UPDATE contract executes the generated optional Phase-2
statement against synthetic CBRE, JLL and marker-collision rows in an isolated
PostgreSQL 17 container. It uses a cached image, publishes no ports, and removes
only its recorded owned container.

The DAUM subset contract runs the actual generated ingest SQL in a uniquely
owned, unexposed cached PostgreSQL 17 container on Linux/WSL. It accepts only a
new proof-directory path, never a database URL. It tests whole-transaction
refusals, held parent/five-child/index preservation, unchanged lifecycle and
baseline/OM data, missing-index/new-insert cases, and bounded fresh replacement
of emitted children. Preserve its checks and container-settlement receipts.

The JLL image-repair contract runs the actual repair SQL against synthetic data
in an owned, unexposed, bounded cached PostgreSQL container. It checks default
read-only comparison, exact delete/rollback/restore, all-table preservation and
concurrent-writer refusals. `--full-scale` adds one synthetic 345,440-image /
9,985-parent case. It never accepts production credentials or a database URL;
see the [operator guide](../../../../docs/firecrawl-ops/references/jll-image-reference-repair.md#isolated-verification)
for the distinct evidence and execution-receipt boundaries.

The Transwestern image-transition contract runs the actual generated
transaction against one owned, unexposed cached PostgreSQL fixture. It accepts
only a new proof directory and `--execute-disposable-fixture`; it never accepts
a database URL or production credentials. Synthetic cases cover classified
noise removal, protected per-parent image multiplicity, primary-role
projection, offstage preservation, full-state digest/readback, and
whole-transaction refusals. The runner's existence or isolated output does not
establish deployment or fresh full-source production acceptance.

**Coverage push (2026-06-15):** a comprehensive unit-test pass lifted production
coverage across the collector (measure with `pytest --cov=. --cov-report=term-missing
--cov-config=/dev/null`, coverage tooling in a throwaway venv). Per-module after:
`cre_validate` 99%, `om_classify_existing` 99%, `cre_geo`/`cre_parse`/`om_url_resolver`
100%, `cre_gate` 96%, `cre_enrich` 96% (all three `\set`/`-f -`/`-q`/`text=True` psql
fixes now regression-guarded in `test_cre_enrich_psql.py`), `om_parse` 92%,
`cre_geo_backfill` 87%, `cre_ingest` 81%, `cre_monitor` 77%, the two backfills
pure-logic 100%. Remaining gaps are the live-psql `main()` / network I/O boundary
and `__main__` guards (intentionally not mocked, per the no-network rule). New test
files: `test_cre_validate.py`, `test_cre_geo_backfill.py`, `test_cre_enrich_psql.py`,
`test_om_parse_sql.py`, `test_om_classify_existing.py`, `test_cre_gate_decisions.py`,
`test_cre_monitor_gaps.py`, `test_cre_ingest_builders.py`, `test_cre_ingest_history.py`,
`test_cre_backfill_raw_data_more.py`, `test_backfill_media_more.py`,
`test_cre_parse_more.py`, `test_om_url_resolver_more.py`, `test_cre_geo_more.py`.

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
| `test_cre_series_status.py` | Bounded series/child status parsing, exact-source detail counters, and log path/generation/privacy guards |
| `test_gate.py` | Gate plus ingest monitor helpers: `norm_status` terminal-wins, `rolling_median`, `select_baseline_updates`, `build_baseline_sql` safety, `rollup_brokerages` |
| `test_monitor.py` | Broad `cre_monitor` coverage: `load_artifact_groups` (errored/truncated fifth-value fold), `coverage_decision` (0.7 boundary, errored not overridable by force), `finalize_group`, `compute_fingerprint`, `derive_events`, `build_write_sql` SQL safety |
| `test_monitor_events.py` | `derive_events` event types, baseline-seed suppression, idempotency, `build_write_sql` safety |
| `test_norm_status_shapes.py` | `norm_status` and `STATUS_SOURCE_PATHS` against gitignored `out/*.json` (skips when absent) |
| `test_norm_status_canonical_and_guards.py` | `_canonical_key`, word-boundary guards, source-classification completeness; no `out/` dependency |
| `test_enum_key_invariant.py` | Enumeration id equals ingest `external_id` for every `SOURCE_TO_BROKERAGE` key |
| `test_ingest_status_activation.py` | OPT-IN status activation: `_status_activation_enabled()`, `apply_status_activation_gate()`, default-off no-op, terminal-stickiness guard |
| `test_env_discovery.py` | `load_db_url` env-file precedence (`--env-file` > `CRE_ENV_FILE` > `~/Documents` defaults), `~` expansion, non-pooling preferred, missing-everywhere exits |
| `test_shell_scripts_syntax.py` | `bash -n` syntax guard over every `*.sh` in the collector (cre_status, cre_run_tier, cre_daily_update, cre_setup, install_launchd, ...); skips if bash absent |
| `test_daily_scripts.py` | Disk-management shell helpers: extracts and runs `prune_keep` (cre_daily_update.sh) and `_keep_newest` (cre_run_tier.sh) in a tmp dir; asserts newest-N retention, no-op under threshold, space-safe `OUT_DIR`, and that the `run_*.json` glob spares `last_run_<tier>.json` markers; skips if bash absent |
| `test_ingest_mark_missing.py` | Folded-coverage `--mark-missing` guard via `cre_ingest.py --dry-run --keep-artifacts` (no DB): parent soft-deletes hold until every sub-source is present (`cbre`+`cbre-dealflow`), fire for complete coverage and singletons (`svn`), and stay blocked when a sub-source pass errored |
| `test_om_facts_migration_contract.py` | Static fresh-runner, default-refusal, and disposable-Postgres contract wiring for legacy migration `015` |
| `test_transwestern_image_outcome.py` | Source-bound receipt, raw-count conservation, exact code/artifact/log binding and full image-state readback |
| `test_transwestern_image_sql.py` | Pure Transwestern role-evidence, SQL-fragment and image-state query contracts |
| `test_transwestern_image_transition_sql.py` | Ingest-writer hook and post-`COMMIT` receipt emission boundary |
| `test_transwestern_image_integration.py` | Clean-code/artifact binding, checkpoint uncertainty refusal, unchanged full raw-query families and supplementary role readback |

The table above lists the foundational contract tests. The data-lift and
freshness/history sessions added ~21 more `test_*.py` files (raw-data / media /
geo backfills, OM parse + enrich, price-history, archive-on-retirement,
dq-guards, transaction-type, revival stickiness, etc.) and the coverage push
added 14 more (see the paragraph above); `HANDOFF_DATA_LIFT_2026-06-15.md`
section 5 has the full list. Run `python3 -m pytest tests/ -q` for the
authoritative current set rather than trusting this table.

`conftest.py` prepends parent `cre_collector/` to `sys.path` (no package install).

## TypeScript unit tests (`tests/ts/`)

Prefer pure transforms calling real functions from `lib/`, `sources/`, or
`types.ts`. Owned loopback fixtures and contained `collect.ts` subprocess
regressions with verified injected transports are supported. No live providers
or Firecrawl calls; preserve the transport-containment rules above.

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
| `lib/enrich.test.ts` | batch claim SQL, URL-keyed completion, dead-letter filtering |
| `lib/geo.test.ts` | `ZipCbsaCrosswalk` (by_zip/by_latlng), `derive_geo` precedence |
| `lib/harvest.test.ts` | media/link harvesting helpers |
| `lib/parse.test.ts` | `parseLeaseRate`, `parseMoney`, `classify_doc` golden-vector parity |
| `sources/savills-commercial.test.ts` | `savillsSaleCardIsCommercial`, `mapSavillsLeaseRow`, `savillsTotalItems` |

Run `npm run test:unit` for the current TypeScript suite and exact test count.

**Argv isolation:** source adapters import `lib/config.ts`; trim `process.argv` to
`[node, script]` before those imports in test files (see `lib/scrape.test.ts`).

Async `src*()` collectors and enrich/fetch helpers may have contained fixture
regressions using the transport rules above. Real provider or Firecrawl calls
remain explicit probes, never ordinary unit-test dependencies.

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

The quality consistency PostgreSQL runner executes the generated active and
historical primary-child conflict queries and the exact generated price/SF
and lease-rate upsert expressions against synthetic rows. It preserves historical child rows,
checks zero/negative/null/positive/boundary/over-limit fallback and valid
replacement behavior, and retains JLL withheld-price and Colliers transition
handling. It accepts no external database URL, uses only a cached image with no network or published ports, and removes only its
created container ID.
