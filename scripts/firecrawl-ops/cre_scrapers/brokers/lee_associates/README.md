# Lee & Associates Scraper Notes

Production bulk collection uses the Buildout inventory path in
`../../../../cre_collector/collect.ts` under source key `lee-associates`.

> STATUS 2026-06-13: COMPLETE. Lee & Associates is fully collected and
> live-ingested (9,223 active rows) through the durable Buildout page cache. The
> "blocked" and "not production-complete" wording in the dated sections below is
> HISTORICAL chronological buildout; the final proof is the "2026-06-12 Full
> Cache Assembly And Live Ingest" section at the end of this file.

## 2026-06-12 Deep Dive Notes

Status: blocked for production completion until a sustained full run proves
every Buildout page. The page failures appear transient rather than permanently
bad pages.

Evidence from bounded probes:

- Prior failure log:
  `scripts/firecrawl-ops/cre_collector/out/lee_latest_2026-06-12_004010.log`
  showed pages 286 through 297 returning non-JSON after 3 attempts.
- Fresh direct GET probe parsed 30 of 30 tested pages, including old failure
  windows 93 through 104 and 286 through 297.
- Fresh local Firecrawl serial probe parsed 24 of 24 tested pages.
- Fresh local Firecrawl window probe parsed pages 260 through 297 at
  concurrency 3, 38 of 38 pages.
- Edge page 332 parsed by direct GET and Firecrawl, with `total=9971`,
  `limit=30`, and `inventoryCount=11`.

Artifacts saved by the probe:

```text
/tmp/lee-buildout-probes-20260612/direct-pages-0-105-285-298.json
/tmp/lee-buildout-probes-20260612/firecrawl-serial-pages-93-104-286-297.json
/tmp/lee-buildout-probes-20260612/firecrawl-window-pages-260-297-concurrency3.json
/tmp/lee-buildout-probes-20260612/page-332-edge-check.json
```

Patch plan:

1. Add a Lee-safe Buildout paging mode that uses direct JSON GET with a browser
   user agent first, then falls back to local Firecrawl only if direct GET
   fails.
2. Add a resumable page cache under gitignored
   `out/cache/buildout/<pluginKey>/page-N.json`, with atomic writes only after
   successful JSON parse.
3. Fetch missing pages in bounded windows, for example 40 pages at concurrency
   2 or 3, with cooldowns between windows.
4. Validate complete page coverage from page 0 through
   `ceil(total / limit) - 1`; abort if any page remains missing.
5. Never cache interstitials or parse failures.
6. Add diagnostic env controls such as `BUILDOUT_PAGE_START` and
   `BUILDOUT_PAGE_END` for future probes.
7. Verify with typecheck, a bounded Lee window, a full Lee JSON artifact, and
   `cre_ingest.py --dry-run`. Do not live ingest Lee until the full artifact is
   clean.

## 2026-06-12 Codex Lee Buildout Probe

Status: bounded Lee probes are now safer, but Lee is not production-complete
until a full Lee-only run proves complete page coverage and passes dry-run
ingest. Do not live ingest Lee and do not use `--mark-missing` from these
bounded probes.

Commands run:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector

npx tsx collect.ts --source=lee-associates --transaction=both --max-items=8 --concurrency=1 --out=/tmp/lee_before_probe.json

npm run typecheck

npx tsx collect.ts --source=lee-associates --transaction=both --max-items=20 --concurrency=1 --out=/tmp/lee_after_probe.json

python3 cre_ingest.py --in /tmp/lee_after_probe.json --dry-run --keep-artifacts /tmp/lee_after_ingest_check
```

Before patch result:

- Wrote `/tmp/lee_before_probe.json`.
- Collected 16 listings, 8 sale and 8 lease, with 7 unique brokers.
- Buildout reported `total=9971`, `limit=30`, 333 pages.
- Firecrawl skipped pages 231, 232, 255, and 256 after three non-JSON
  attempts each, then cached 9,851 of 9,971 inventory rows. This was not safe
  enough for production because the artifact could be gappy while reporting
  source success.

Direct GET evidence:

- `/tmp/lee_direct_buildout_probe_20260612.json`: initial direct Node GET to
  pages 0, 231, 232, 255, 256, and edge page 332 returned `403 text/html`
  with a small HTML forbidden body, not JSON.
- `/tmp/lee_after_failure_direct_http_shape_20260612.json`: immediately after
  one failed collector pass, direct GET to pages 313, 314, and 332 returned
  `200 application/json`, keys `inventory,meta`, `total=9971`, `limit=30`;
  page 332 had `inventoryCount=11`.

Patch applied in `collect.ts` Buildout helper:

- Adds optional direct JSON fetch with browser headers and source referer.
- Falls back to Firecrawl when direct GET returns HTML or other non-JSON.
- Tracks failed page numbers rather than only a count.
- Adds a bounded recovery pass that can retry failed or skipped Lee pages after
  a cooldown.
- Requires complete Lee page coverage before caching or writing a successful
  Lee artifact.
- Leaves the Buildout behavior option-driven so the Lee recovery bound is not
  applied broadly to other brokers.

After patch verification:

- `npm run typecheck` passed.
- First `--max-items=20` after-probe failed closed, with no
  `/tmp/lee_after_probe.json` artifact, after page 313 pushed the failed set to
  29 of 333 pages. This verified that Lee no longer writes a gappy success
  artifact when page coverage is incomplete.
- Final `--max-items=20` after-probe wrote `/tmp/lee_after_probe.json`.
  It collected 40 listings, 20 sale and 20 lease, with 24 unique brokers.
  Pages 230, 231, and 232 failed through direct GET plus Firecrawl fallback,
  then recovered in the bounded recovery pass.
- Final inventory cache was complete: 9,971 items cached against source
  `total=9971`.
- Dry-run ingest staged 40 Lee rows, skipped 0 rows for missing URL, and wrote
  `/tmp/lee_after_ingest_check/ingest.sql`.

Recommendation:

- The Buildout transient failure can be addressed safely for Lee bounded probes
  with direct-first fetch, Firecrawl fallback, bounded recovery, and strict
  complete-page validation.
- A full Lee-only collector run is now a reasonable next test after the current
  Cushman workload is finished. Use `--concurrency=1`, do not live ingest from
  the first full Lee artifact, and do not use `--mark-missing` until a full
  clean Lee artifact plus `cre_ingest.py --dry-run` pass.

## 2026-06-12 Durable Cache And Window Controls

Status: tooling implemented and bounded cache-fill probes started. Lee is still
not production-complete and has not been live-ingested.

Collector controls added to the shared Buildout helper, enabled by default only
for Lee & Associates:

- Durable page cache under `out/cache/buildout/lee-associates/page-0000.json`.
  The cache directory is gitignored and stores successful JSON pages only.
- Atomic page writes via temp file plus rename.
- `BUILDOUT_CACHE_ONLY=1`: fetches the requested window into cache and then
  intentionally refuses to write a partial listing artifact.
- `BUILDOUT_PAGE_START` and `BUILDOUT_PAGE_END`: select a page window.
- `BUILDOUT_ASSEMBLE_FROM_CACHE=1`: assembles only from cached pages and fails
  if any expected page is missing. Recovery and network fallback are disabled in
  this mode.
- `BUILDOUT_PAGE_JITTER_MS=min,max`: adds per-page jitter before page fetches.
- Diagnostics now distinguish attempted, failed, and unattempted pages after an
  abort.

Verification commands:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npm run typecheck
BUILDOUT_CACHE_ONLY=1 BUILDOUT_PAGE_START=0 BUILDOUT_PAGE_END=3 BUILDOUT_PAGE_JITTER_MS=200,500 \
  npx tsx collect.ts --source=lee-associates --transaction=both --max-items=0 --concurrency=1 \
  --out=/tmp/lee_cache_window_probe_should_not_write.json
BUILDOUT_ASSEMBLE_FROM_CACHE=1 \
  npx tsx collect.ts --source=lee-associates --transaction=both --max-items=0 --concurrency=1 \
  --out=/tmp/lee_assemble_incomplete_should_fail.json
```

Observed results:

- Typecheck passed.
- Cache-only window 0-3 fetched and cached the selected pages, then exited with
  `no listings collected from any source` by design because partial artifacts
  are forbidden.
- Initial assemble-from-cache test exposed and fixed a bug where the recovery
  branch could bypass cache-only mode. After the fix, assemble mode fails on
  missing pages without network fallback.
- Cache-fill windows 14-23, 24-33, 34-43, 44-53, 54-63, and 64-73 completed
  with 0 selected pages missing.
- Current cached page coverage after the first batch: contiguous pages 0-73,
  74 pages total. Page 0 reported `total=9975`, `limit=30`, so full assembly
  currently expects 333 pages.

Safe continuation:

1. Keep filling small windows with `BUILDOUT_CACHE_ONLY=1`, jitter, and a
   cooldown between windows.
2. Periodically count cached pages and missing page ranges.
3. Run `BUILDOUT_ASSEMBLE_FROM_CACHE=1` only after pages 0 through 332 are
   cached.
4. Only after a complete assembled Lee artifact exists, run
   `cre_ingest.py --dry-run`.
5. Do not live ingest Lee and do not use source-scoped `--mark-missing` until
   the full assembled artifact and dry-run are clean.

## 2026-06-12 Full Cache Assembly And Live Ingest

Status: Lee & Associates is now complete for the defensible public Buildout
inventory feed and live-ingested with source-scoped reconciliation.

Cache-fill result:

- Cached pages: 333 of 333, contiguous pages 0 through 332.
- Cache size: about 37 MB.
- Page 0 metadata: `total=9975`, `limit=30`.
- Edge page 332 metadata: `total=9975`, `limit=30`, 15 inventory rows.
- The old failure window around pages 286 through 297 was filled successfully
  through cache-only windows.

Assembly command:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
BUILDOUT_ASSEMBLE_FROM_CACHE=1 \
  npx tsx collect.ts --source=lee-associates --transaction=both --max-items=0 --concurrency=1 \
  --out=out/lee_full_cache_2026-06-12_assembled.json
```

Artifact result:

- Artifact: `out/lee_full_cache_2026-06-12_assembled.json`, 8.9 MB.
- Log: `out/lee_full_cache_2026-06-12_assembled.log`.
- Raw rows: 9,975.
- Sale rows before merge: 3,447.
- Lease rows before merge: 6,528.
- Unique run-level brokers: 1,085.
- Missing URLs: 0.
- Missing titles: 0.
- Closed rows: 0.
- URL-only document rows in artifact: 8,238.
- Image URLs in artifact: 9,975.
- Broker references in artifact: 9,975.

Ingest behavior:

- Dry-run staged 9,223 unique rows and skipped 0 missing URLs.
- The 752-row reduction is expected. `cre_ingest.py` strips `-sale` and
  `-lease` from Buildout URL `propertyId` values, merging 744 sale+lease
  property pairs into `sale_or_lease` plus 8 exact duplicate rows.
- Source-scoped `--mark-missing` was dry-run and then live-applied only for
  `lee-associates`.

Supabase proof:

- Active Lee rows: 9,223.
- Transaction split: 2,611 sale, 5,691 lease, 921 sale_or_lease.
- Child rows: 9,062 image URL rows, 7,681 document URL rows, 9,223 contact
  rows.
- Quality checks: 0 bad source URLs, 0 missing titles, 0 missing raw data, 0
  invalid states, 0 impossible coordinates, 0 malformed guarded prices/cap
  rates, 0 duplicate external IDs, 0 bad child URLs, and 0 child orphans.
- Search proof: the live five-argument
  `credeals.search_cre_listings('Lee', null, null, null, null)` returned Lee &
  Associates rows.

Remaining limit:

- The collector uses Buildout feed-level data and broker records exposed by the
  public inventory JSON. It does not scrape separate Lee detail pages, VCards,
  or richer galleries unless those become available through a safe public path.
