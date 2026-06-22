Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# Lee & Associates Buildout Throttling And Resumability Deep Dive

Date: 2026-06-12 local time.

Scope: Lee & Associates Buildout collector behavior only. No live ingest was
run. No PDFs or images were downloaded. Probe commands below inspected JSON
response shape only.

## Current Verdict

Lee is not ready for production reconciliation or `--mark-missing`.

The strongest hypothesis is stateful Buildout throttling during sustained
inventory paging, not a permanently bad page range. Individual pages that
failed in earlier collector runs can return valid JSON later, but long runs can
start receiving 403 HTML or other non-JSON bodies after enough sequential or
parallel requests. Because the collector must fetch all 333 inventory pages
before sale/lease filtering or `--max-items` slicing, even a tiny Lee probe can
still become a full sustained Buildout crawl.

## Evidence Reviewed

- `cre_collector/START_HERE.md`: latest system status says Lee produced 0 rows
  and must remain excluded from mark-missing workflows.
- `cre_collector/CLAUDE.md`: Buildout feeds for SVN and Lee have no usable
  server-side sale/lease filter. The collector fetches full inventory once per
  brokerage, caches it in memory, partitions sale/lease client-side, and must
  fail closed when page coverage is incomplete.
- `cre_collector/VALIDATION_2026-06-12.md`: Lee-only run failed pages 286
  through 297 after retries and produced no JSON artifact.
- `cre_collector/LESSONS_2026-06-11.md`: Buildout can return HTML interstitials
  after many pages; isolated page retries are not enough for safe
  reconciliation.
- `cre_collector/BROKERAGE_STATUS_2026-06-12.md`: Lee remains blocked, with
  sustained full run failure around pages 286 through 297.
- `cre_collector/out/lee_latest_2026-06-12_004010.log`: pre-direct-first Lee run
  reached page 275, then pages 286 through 297 returned non-JSON after three
  attempts each. Sale failed closed and lease reused the cached failure.
- `cre_collector/out/lee_buildout_direct_probe_2026-06-12_041408.log`: a later
  direct-first run saw page 0 and page 25 succeed, then direct GET returned
  403 text/html from page 32 onward and Firecrawl fallback also returned
  non-JSON. This shows the failure window can move much earlier once the
  endpoint is in a throttled state.
- `cre_scrapers/brokers/lee_associates/README.md`: earlier bounded probes
  showed old failure windows and edge page 332 can parse when tested later,
  and current code was already hardened to direct-first, Firecrawl fallback,
  one recovery pass, and complete-page validation.

## Current Code Behavior

Relevant code: `scripts/firecrawl-ops/cre_collector/collect.ts`.

- Buildout helper lives at lines 459 through 634.
- Lee registration lives at lines 3909 through 3925.
- `--max-items` is parsed at lines 82 through 83 and passed to `srcBuildout`,
  but `srcBuildout` receives the already completed full inventory. Therefore
  `--max-items=2` does not reduce Buildout page pressure.
- `--page-cap` is parsed at line 84 but is not used by Buildout inventory
  pagination.
- Lee currently uses `preferDirectJson: true`, `pageConcurrency: 1`,
  `requireCompletePages: true`, `recoveryPasses: 1`,
  `recoveryCooldownMs: 15000`, and `maxRecoveryPages: 60`.
- The in-memory `buildoutCache` prevents duplicate sale and lease fetches
  within one process only. It does not survive a failed process or allow page
  fill-in across sessions.
- The current `aborting` branch marks later unattempted pages as failed after
  the failure threshold is crossed. That is acceptable for fail-closed safety,
  but not useful for resumability diagnostics because it mixes blocked pages
  with pages that were never attempted.

## Bounded Probe Results From This Pass

Commands were run from repo root unless noted.

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Result: passed. Docker compose printed expected unset optional env warnings.
API root check and scrape smoke test passed.

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
```

Result: passed.

```bash
node --input-type=module -e '<five-page direct Buildout shape probe>'
```

Pages tested: 0, 32, 286, 297, 332.

Result: all five returned `200 application/json`, keys `inventory,meta`,
`limit=30`, and `total=9972`. Pages 0, 32, 286, and 297 each had 30 inventory
rows. Edge page 332 had 12 inventory rows. This is consistent with a moving
throttle state rather than fixed bad pages. It also shows the Lee source total
changed from earlier notes reporting 9971 to 9972.

```bash
node --input-type=module -e '<one-page local Firecrawl scrape shape probe>'
```

URL: `https://buildout.com/plugins/9a64a93980aeae8db347e72cdfa8ca61017acc9a/inventory.json?page=286`

Result: local Firecrawl returned success with parseable raw JSON, keys
`inventory,meta`, `inventoryCount=30`, `total=9972`, and `limit=30`.

## Safer Patch Proposal

Do not make the next change a full Lee run. Make it a Lee-enabled Buildout
resumability layer, then prove it with small fill-cache windows.

1. Add a durable page cache, opt-in for Lee first.

   Proposed cache path:

   ```text
   scripts/firecrawl-ops/cre_collector/out/cache/buildout/lee-associates/page-0000.json
   ```

   Requirements:

   - Only write after JSON parse succeeds and `inventory` is an array.
   - Write atomically through a temp file plus rename.
   - Store a small wrapper with `pluginKey`, `company`, `page`, `fetchedAt`,
     `meta.total`, `meta.limit`, and `inventory`.
   - Reject cache entries whose plugin key, page, or limit disagree with the
     current run.
   - Never cache 403 HTML, non-JSON bodies, empty parse failures, or negative
     results.
   - Treat total drift as a warning. If total changes, keep valid page JSON but
     recompute expected page count from page 0 and refresh the edge page.

2. Add true Buildout page windows for probes and cache fill.

   Candidate env controls:

   ```text
   BUILDOUT_PAGE_START=0
   BUILDOUT_PAGE_END=39
   BUILDOUT_CACHE_ONLY=1
   BUILDOUT_CACHE_DIR=out/cache/buildout
   ```

   The collector should be able to fetch a bounded page window, save page cache
   entries, and exit without writing a listing artifact unless the full page set
   is complete.

3. Add pacing before fallback.

   Current direct-first behavior immediately falls back to Firecrawl on direct
   403 for each page. When throttling starts, that doubles request pressure.
   Safer behavior:

   - Fetch serially for Lee.
   - Add jitter between page requests, for example 1500 to 3500 ms.
   - Process pages in windows of 10 to 20.
   - Cool down 60 to 120 seconds between windows.
   - On the first non-JSON response in a window, pause the window and record the
     page for later recovery rather than hammering direct plus Firecrawl across
     the next pages.
   - Try the alternate transport only in recovery, after cooldown, one page at
     a time.

4. Split attempted, failed, and unattempted pages.

   Replace the current `aborting` behavior that adds every later page to
   `failedPages` with separate sets:

   ```text
   attemptedPages
   failedPages
   blockedAfterPage
   missingPages = expectedPages - cachedPages
   ```

   This makes logs and recovery plans accurate.

5. Make full success stricter than cache fill.

   Lee source success requires:

   - Page 0 fetched fresh enough to know current `total` and `limit`.
   - Every page from 0 through `ceil(total / limit) - 1` present in cache or
     fetched in the current run.
   - Combined inventory length equals the sum of all cached page inventory row
     counts, and is close to source `total` after accounting for edge page size.
   - No missing pages.
   - Only then should sale/lease partitioning run and an artifact be written.

6. Keep Lee production run conservative.

   First full proof should use Lee only, no live ingest:

   ```bash
   cd scripts/firecrawl-ops/cre_collector
   BUILDOUT_CACHE_DIR=out/cache/buildout \
   BUILDOUT_WINDOW_SIZE=10 \
   BUILDOUT_WINDOW_COOLDOWN_MS=90000 \
   BUILDOUT_PAGE_JITTER_MS=1500,3500 \
   npx tsx collect.ts --source=lee-associates --transaction=both \
     --max-items=0 --concurrency=1 --out=out/lee_full_resumable_2026-06-12.json
   python3 cre_ingest.py --in out/lee_full_resumable_2026-06-12.json \
     --dry-run --keep-artifacts /tmp/lee_full_resumable_2026-06-12_ingest_check
   ```

   Do not live ingest from the first full artifact until the page coverage
   summary, staged row count, and dry-run SQL are inspected.

## Endpoint And Query Alternatives Considered

- `?sale=true` appears in the older Prometheus script, but current collector
  docs and Buildout semantics say sale/lease filtering is ignored by the
  inventory feed. It does not reduce page count safely.
- `--max-items` does not reduce inventory page pressure because filtering and
  slicing happen after full inventory fetch.
- `--page-cap` does not currently apply to Buildout.
- A smaller full source cap would be unsafe for production because it would
  create partial inventory while the source reports about 9972 rows.
- Direct GET and local Firecrawl both work for individual pages right now, so
  the problem is sustained access pattern rather than endpoint discovery.

## Next Safe Full-Run Plan

1. Patch only the Lee/Buildout helper with durable page cache, page-window
   controls, pacing, and accurate attempted versus unattempted diagnostics.
2. Run `npm run typecheck`.
3. Fill cache with small windows, starting with pages 0 through 19, then 20
   through 39, using at least 60 seconds between windows.
4. Inspect cache manifest and confirm no non-JSON entries were written.
5. Run a no-ingest Lee-only full assembly from cache plus missing page fill.
6. Run `cre_ingest.py --dry-run --keep-artifacts`.
7. Only after a clean artifact and dry-run, decide whether to run additive live
   ingest. Keep `--mark-missing` off until Lee has been clean in an all-source
   run and the source-specific guards are acceptable for that day.

