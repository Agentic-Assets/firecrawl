# CRE listing full refresh progress

## 2026-09-08 preflight

- Production data is stale: all 51 source freshness generations predate the
  24-hour freshness target. The live read-only validation found 116,058 active
  listings and passed its integrity checks.
- The prior checkpoint series are more than 28 days old and cannot be resumed.
- The local Firecrawl API and Playwright stack were restored and passed the
  repository health check, including a scrape smoke test.
- `npm run typecheck` passed.
- `npm run test:unit` passed with 768 tests.
- The Python collector suite initially exposed 12 test-fixture failures: one
  clone-dependent gitignored fixture and eleven fixed-date freshness fixtures.
  The tests now skip only when the optional reviewed artifact is absent and use
  a current timezone-aware observation window.
- The repaired Python suite passed with 2,130 tests and one intentional skip.
- Changed Python tests pass Ruff F/I checks, `py_compile`, and `git diff --check`.

## Current gate

Commit and push the deterministic test repair, then start a new strict
all-source checkpoint series from that exact clean SHA.

