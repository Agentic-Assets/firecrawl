# tests Module

## Most Critical Rule

**Pure-transform, no-network tests only.** Import and call real functions from `cre_ingest.py`, `cre_gate.py`, and `cre_monitor.py`. No live DB, Supabase, or Firecrawl. Use synthetic JSON and temp files unless a file header says otherwise.

**Observe-only SQL invariant:** `build_write_sql` (`cre_monitor.py`) and `build_baseline_sql` (`cre_gate.py`) must never assign `cre_listings.status` or `cre_listings.deleted_at`. Tests grep generated SQL (non-comment lines) to enforce this. `cre_ingest.py` mark-missing SQL is out of scope here.

**Re-implementation rule:** Do not duplicate production logic in assertions. Exception: `test_enum_key_invariant.py` defines `monitor_enum_key()` as the explicit contract mirror for enumeration id vs `to_row().external_id`.

## Folder-Specific Commands

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/ -q
python3 -m pytest tests/test_enum_key_invariant.py -q
python3 -m pytest tests/test_norm_status_canonical_and_guards.py -q   # portable CI signal (no out/)
```

Requires `pytest` on the host (not pinned in `package.json`). `collect.ts` coverage: `npm run typecheck` (parent dir, not this folder).

## Test Files

| File | Focus |
|------|-------|
| `test_cre_gate.py` | `cre_gate`: `verdict_for`, `rolling_median`, `select_baseline_updates`, `count_artifacts` |
| `test_gate.py` | Gate plus ingest monitor helpers: `norm_status` terminal-wins, `rolling_median`, `select_baseline_updates`, `build_baseline_sql` safety, `rollup_brokerages` |
| `test_monitor.py` | Broad `cre_monitor` coverage (some gate overlap): `load_artifact_groups`, `finalize_group`, `compute_fingerprint`, `derive_events`, `build_write_sql` SQL safety |
| `test_monitor_events.py` | `derive_events` event types, baseline-seed suppression, idempotency, `build_write_sql` safety |
| `test_norm_status_shapes.py` | `norm_status` and `STATUS_SOURCE_PATHS` against gitignored `out/*.json` (skips when absent) |
| `test_norm_status_canonical_and_guards.py` | `_canonical_key`, word-boundary guards, source-classification completeness; no `out/` dependency |
| `test_enum_key_invariant.py` | Enumeration id equals ingest `external_id` for every `SOURCE_TO_BROKERAGE` key |

`conftest.py` prepends parent `cre_collector/` to `sys.path` (no package install).

## Module Boundaries

Owns Python ingest, monitor, and gate unit contracts. Does **not** own E2E `collect.ts` runs, live `cre_ingest.py --apply`, or `cre_validate.py` / `npm run validate:supabase`.

Overlap across `test_gate.py`, `test_cre_gate.py`, and `test_monitor.py` is intentional regression coverage.

## Integration Points

- **`cre_ingest`**: `to_row`, `merge_rows`, `norm_status`, `_canonical_key`, `SOURCE_TO_BROKERAGE`, `STATUS_SOURCE_PATHS`
- **`cre_gate`**: `verdict_for`, `rolling_median`, `select_baseline_updates`, `build_baseline_sql`, `rollup_brokerages`, `count_artifacts`
- **`cre_monitor`**: `load_artifact_groups`, `finalize_group`, `compute_fingerprint`, `derive_events`, `build_write_sql`
- Id prefix, status paths, gate floors, or observe-only SQL changes need matching test updates in the same PR.

## References

- `../cre_ingest.py`, `../cre_gate.py`, `../cre_monitor.py`
- `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
- `../../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` (especially sections 6, 8, 9, 12, 14.4)
