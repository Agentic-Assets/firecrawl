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

## 2026-09-09 recovery

- The first strict series, `2026-09-09T010142Z`, collected and admitted 21,147
  CBRE rows but failed before commit because production lacked migration 016's
  `cre_scrape_jobs.artifact_run_key`. The ingest transaction rolled back and
  generation-exact recovery found no partial persisted generation.
- Cayman explicitly approved migration 016 under `AGENTIC-1229`.
- Applied `016_cre_listing_lifecycle.sql` at SHA-256
  `112e7acc30245fa90cdaffbc8b5dfa219f6ca1af07379ecd84ba4785cfba9e7e`.
  The migration committed in one transaction and initialized 105,050 source
  index rows.
- Required production readback passed for all lifecycle columns, constraints,
  four partial unique indexes, and both `ON DELETE SET NULL` foreign keys.
- Post-migration validation remained green with 116,058 active listings, zero
  duplicate external IDs, zero child orphans, and zero invalid child URLs.
- Both EQUIRE and Corbis public front doors returned HTTP 200 after redirects.
- Supabase schema lint found inherited errors in unrelated `credeals`
  functions; it found no migration-016 defect. Those unrelated application
  objects were not changed.
- Added a read-only, 11-item migration-016 schema contract to validation and a
  strict fail-fast gate before source collection. The full Python collector
  suite passed with 2,138 tests and one expected clone-dependent skip; focused
  schema-gate coverage passed 333 tests.
- The default Colliers Main calibration was stopped after it conclusively
  missed admission: 91 requests in 40 minutes with retries, versus the required
  13.2 clean details per minute. A two-second render-wait candidate also stayed
  near 2.2 details per minute, proving the bottleneck was the one-context-per-
  request transport rather than the configured render delay.
- A read-only live probe found the current public Colliers Sitecore/Coveo search
  surface can return exactly the 15,944 US property IDs in the sitemap without
  authentication. Its records expose the current scalar, contact, document,
  image, and revision fields. Exact ID reconciliation, payload mapping, and
  fail-closed admission tests are in progress before this can replace the slow
  rendered-detail path.
- CBRE Deal Flow now preserves a freshly observed canonical inventory card when
  its optional linked detail is unavailable, records the explicit
  `detail_request_failed` reason, and preserves existing child collections.
  Identity, structured-mapping, pagination, cardinality, and uniqueness failures
  still fail closed. Focused TypeScript, checkpoint, and ingest tests passed.

## Current gate

Finish exact Colliers sitemap/API reconciliation and field-parity verification,
commit and push the source fixes and fail-fast schema gate, then start a new
strict all-source checkpoint series from the new exact SHA.
