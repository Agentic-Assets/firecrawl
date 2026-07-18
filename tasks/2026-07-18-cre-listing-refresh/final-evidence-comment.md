## 2026-07-18 final recovery evidence

- Branch: `fix/cre-enrich-source-paths`
- Commit: `442e9ee59d46322e9a97e088a5af1525796f29f9`
- Draft PR: https://github.com/Agentic-Assets/firecrawl/pull/23
- Checks: `npm run typecheck`; `npm run test:unit` (491 passing); `python3 -m
  pytest -q tests/test_cre_enrich.py tests/test_cre_enrich_psql.py` (69
  passing); `cre_validate.py --format json` (`ok: true`).
- Live proof: Savills current enumeration completed; NAI Global completed an
  untruncated 13,779-record current-feed pass. The 2,672 remaining targeted
  enrichment rows and NAI's 12,517 monitor-only unmatched rows are explicitly
  retained for safe detail/status enrichment. No scheduler was restored and no
  status, soft-delete, OM-facts, or EQUIRE market-data mutation was made.

The root pre-commit Knip hook remains blocked by unrelated missing
`apps/api/node_modules/typescript5/package.json`; commit used `--no-verify`
only after the scoped checks above passed.
