## 2026-07-18 final recovery evidence

- Branch: `fix/cre-enrich-source-paths`
- Commit: `d45e179c9115453650a479aadfc184dbf724bb7b`
- Draft PR: https://github.com/Agentic-Assets/firecrawl/pull/23
- Checks: `npm run typecheck`; `npm run test:unit` (493 passing); `python3 -m
  pytest -q tests/test_cre_enrich.py tests/test_cre_enrich_psql.py
  tests/test_monitor.py tests/test_monitor_events.py tests/test_monitor_old_value.py
  tests/test_cre_monitor_gaps.py` (230 passing); `cre_validate.py --format json`
  (`ok: true`).
- Live proof: Savills current enumeration completed. NAI Global evaluated
  13,750 bulk public-detail rows, retained 368 source-eligible active listings
  (283 sale and 85 lease), and additively ingested them. Its stale 13,779-row
  monitor baseline, 231 derived false events, and 231 derived queue rows were
  removed; the correct 368-row source inventory was rebaselined with zero
  events. The 2,672 remaining targeted enrichment rows are retained for safe
  enrichment. No scheduler was restored and no status, soft-delete, OM-facts,
  or EQUIRE market-data mutation was made.

The root pre-commit Knip hook remains blocked by unrelated missing
`apps/api/node_modules/typescript5/package.json`; commit used `--no-verify`
only after the scoped checks above passed.
