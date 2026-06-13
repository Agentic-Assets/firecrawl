# tests Module

## Most Critical Rule

**Pure-transform, no-network tests only.** Call real functions from `cre_ingest.py`, `cre_gate.py`, and `cre_monitor.py`; never re-implement logic. **No DB, Supabase, or Firecrawl.** SQL assertions must prove generated SQL never touches `cre_listings.status` or `deleted_at`.

## Folder-Specific Commands

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/ -q
python3 -m pytest tests/test_enum_key_invariant.py -q   # enum id == ingest external_id
```

`collect.ts` coverage: `npm run typecheck` (not this folder).

## Naming Patterns

- `test_*.py` by subsystem: `cre_gate`/`gate`, `monitor`/`monitor_events`, `norm_status_*`, `enum_key_invariant`.
- `conftest.py` prepends parent dir to `sys.path` (no package install).
- Synthetic fixtures unless the file header says otherwise.

## Module Boundaries

Owns Python ingest/monitor/gate unit contracts. Does **not** own E2E collect, live ingest, or `cre_validate.py`.

`test_norm_status_shapes.py` reads gitignored `out/*.json` and skips when absent. `test_norm_status_canonical_and_guards.py` is the portable signal with no `out/` dependency.

## Integration Points

- **`cre_ingest`**: `to_row`, `merge_rows`, `norm_status`, `SOURCE_TO_BROKERAGE`
- **`cre_gate`**: `verdict_for`, baseline seed/update, brokerage rollup
- **`cre_monitor`**: `derive_events`, `finalize_group`, `build_write_sql`
- Id prefix, status path, or gate-floor changes need matching test updates in the same PR.

## References

- `../cre_ingest.py`, `../cre_gate.py`, `../cre_monitor.py`
- `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
- `../../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` (sections 6, 8, 9, 12)
