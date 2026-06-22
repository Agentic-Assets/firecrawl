Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# Lee & Associates Buildout Subagent Findings

Date: 2026-06-12 local time.

Scope: Lee & Associates only. No live ingest was run. No collector full run was
started. No PDFs, images, or other binaries were downloaded.

## Verdict

Lee still looks blocked by sustained Buildout paging, not by endpoint discovery
or permanently bad page numbers.

The current collector is safer than the original failure mode because Lee now
uses direct JSON first, Firecrawl fallback, one recovery pass, and strict
complete-page validation. It is not yet resumable. It still has no durable page
cache, no page-window mode, no pacing jitter, and no cooldown between normal
page batches. A small `--max-items` collector run can still fetch every
Buildout inventory page before slicing final listings.

Do not live ingest Lee and do not use Lee for `--mark-missing` until a sustained
full Lee artifact is complete and `cre_ingest.py --dry-run` passes.

## Files Read

- `CLAUDE.md`
- `scripts/firecrawl-ops/CLAUDE.md`
- `scripts/firecrawl-ops/cre_scrapers/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/HANDOFF_LOG_2026-06-11.md`
- `scripts/firecrawl-ops/cre_collector/LESSONS_2026-06-11.md`
- `scripts/firecrawl-ops/cre_collector/VALIDATION_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/lee_associates/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/lee_associates/LEE_BUILDOUT_THROTTLING_RESUMABILITY_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/collect.ts`

`START_HERE.md` exists under `cre_collector/`. No root-level `START_HERE.md`
was present in this checkout.

## Current Implementation Shape

Relevant code is in `scripts/firecrawl-ops/cre_collector/collect.ts`.

- Buildout helper: `buildoutInventory()` and `fetchBuildoutInventoryPage()`.
- Lee source registration: source key `lee-associates`, plugin key
  `9a64a93980aeae8db347e72cdfa8ca61017acc9a`.
- Lee options: `preferDirectJson: true`, `pageConcurrency: 1`,
  `requireCompletePages: true`, `recoveryPasses: 1`,
  `recoveryCooldownMs: 15000`, `maxRecoveryPages: 60`.
- `buildoutCache` and `buildoutFailureCache` are process-local only. They avoid
  duplicate sale and lease fetches inside one run but do not support restart,
  resume, or cache fill across sessions.
- `--max-items` is applied after `srcBuildout()` receives full inventory.
  It limits final sale or lease listing rows, not Buildout page requests.
- `--page-cap` is parsed for rendered-page sources but does not apply to
  Buildout inventory pages.
- When too many pages fail, the `aborting` branch adds later pages to
  `failedPages` without distinguishing attempted pages from unattempted pages.
  That is safe for fail-closed behavior, but weak for diagnostics and resume
  planning.

## Historical Evidence

Observed logs show moving failure windows:

- `out/full_latest_2026-06-11_230423.log`: Lee began returning non-JSON around
  pages 93 through 101 in the all-source run.
- `out/lee_latest_2026-06-12_004010.log`: a Lee-only full run reached page 275,
  then pages 286 through 297 returned non-JSON after three attempts each. Sale
  failed closed and lease reused the cached failure.
- `out/lee_buildout_direct_probe_2026-06-12_041408.log`: a later direct-first
  bounded collector probe got page 0 and page 25, then direct GET returned
  `403 text/html` starting at page 32. Firecrawl fallback also returned
  non-JSON across the same window.

This pattern is consistent with stateful throttling or temporary challenge
responses under sustained pagination. It is not consistent with fixed bad page
numbers.

## Safe Probes Run In This Pass

From repo root:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Result: failed before API checks because OrbStack/Docker was not reachable at
`/Users/caymanseagraves/.orbstack/run/docker.sock`. The Docker context is still
`orbstack`, but local Firecrawl was not available on `localhost:3002`.

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
```

Result: passed.

```bash
curl -sS -m 5 http://localhost:3002/
```

Result: failed to connect, consistent with the healthcheck failure.

```bash
docker context show
```

Result: `orbstack`.

Direct Buildout JSON shape probe, with browser user agent, Lee referer, and
1.5 second delay between pages:

```bash
node --input-type=module <<'NODE'
const plugin = '9a64a93980aeae8db347e72cdfa8ca61017acc9a';
const pages = [0, 32, 93, 286, 297, 332];
for (const page of pages) {
  const url = `https://buildout.com/plugins/${plugin}/inventory.json?page=${page}`;
  const started = Date.now();
  const res = await fetch(url, {
    headers: {
      accept: 'application/json,text/plain,*/*',
      referer: 'https://www.lee-associates.com/properties/',
      'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36',
    },
  });
  const contentType = res.headers.get('content-type') || '';
  const text = await res.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch {}
  console.log(JSON.stringify({
    page,
    status: res.status,
    contentType,
    elapsedMs: Date.now() - started,
    parseableJson: !!parsed,
    total: parsed?.meta?.total ?? null,
    limit: parsed?.meta?.limit ?? null,
    inventoryCount: Array.isArray(parsed?.inventory) ? parsed.inventory.length : null,
  }));
  await new Promise((resolve) => setTimeout(resolve, 1500));
}
NODE
```

Result summary:

| Page | Status | JSON | Total | Limit | Inventory rows |
|---:|---:|---|---:|---:|---:|
| 0 | 200 | yes | 9976 | 30 | 30 |
| 32 | 200 | yes | 9976 | 30 | 30 |
| 93 | 200 | yes | 9976 | 30 | 30 |
| 286 | 200 | yes | 9976 | 30 | 30 |
| 297 | 200 | yes | 9976 | 30 | 30 |
| 332 | 200 | yes | 9976 | 30 | 16 |

The total has drifted from prior notes: older probes saw 9971 or 9972, this
pass saw 9976. That makes page 0 metadata validation and edge-page refresh
important for any durable cache.

## Why Full Collection Is Slow Or Failing

1. Lee requires about 333 Buildout page requests before any sale/lease slice can
   be emitted. At 30 rows per page and current total 9976, even a tiny
   collector probe has full-feed pressure.
2. Prior logs show Buildout can start serving `403 text/html` or other non-JSON
   bodies after sustained page access. The failure window moves between runs.
3. Current direct-first behavior can double pressure during a throttle event:
   direct GET fails, then Firecrawl fallback immediately tries the same page.
4. Current recovery waits 15 seconds and retries failed pages, but it cannot
   preserve successful pages across process restarts or gradually fill missing
   windows.
5. `pageConcurrency: 1` reduces parallel pressure, but the normal loop still
   walks hundreds of pages without per-page jitter or batch cooldown.
6. Diagnostics currently conflate failed pages with unattempted pages after the
   failure threshold is crossed.

## Recommended Patch Plan

Patch only the shared Buildout helper in a Lee-opt-in way first. Keep SVN
behavior unchanged unless the new controls are deliberately enabled there too.

1. Add durable page cache controls:
   - `BUILDOUT_CACHE_DIR`, defaulting to
     `scripts/firecrawl-ops/cre_collector/out/cache/buildout`.
   - Cache path:
     `out/cache/buildout/lee-associates/page-0000.json`.
   - Atomic temp-write plus rename.
   - Cache only successful JSON with `inventory` array and matching page,
     plugin key, and limit.
   - Never cache HTML, non-JSON, parse failures, empty error bodies, or
     negative results.

2. Add Buildout page-window controls:
   - `BUILDOUT_PAGE_START`
   - `BUILDOUT_PAGE_END`
   - `BUILDOUT_CACHE_ONLY=1`
   - `BUILDOUT_ASSEMBLE_FROM_CACHE=1`
   - A window should fill cache and exit without writing a listing artifact
     unless all required pages are present.

3. Add Lee pacing:
   - Per-page jitter, for example `BUILDOUT_PAGE_JITTER_MS=1500,3500`.
   - Window size, for example `BUILDOUT_WINDOW_SIZE=10`.
   - Window cooldown, for example `BUILDOUT_WINDOW_COOLDOWN_MS=90000`.
   - On first 403 or non-JSON response in a window, stop that window and defer
     alternate transport to a later recovery pass.

4. Split page state:
   - `attemptedPages`
   - `failedPages`
   - `blockedAfterPage`
   - `missingPages`
   This will make logs, resume reports, and patch validation much clearer.

5. Add strict assembly validation:
   - Fetch page 0 fresh enough to determine `total`, `limit`, and expected page
     count.
   - Refresh the edge page when total changes.
   - Require every page 0 through `ceil(total / limit) - 1` in the cache or in
     the current run before sale/lease partitioning.
   - Report cache coverage and missing page ranges.

6. Verification path:
   - `npm run typecheck`
   - Fill pages 0 through 9 only with `BUILDOUT_CACHE_ONLY=1`.
   - Fill pages 10 through 19 after cooldown.
   - Inspect cache manifest or page files for JSON-only content.
   - Assemble from cache, expecting a fail-closed result until all pages are
     present.
   - Only after all pages are filled, run Lee-only no-ingest full artifact and
     `python3 cre_ingest.py --in <artifact> --dry-run --keep-artifacts <dir>`.

## Next Safe Commands After Patch

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npm run typecheck
BUILDOUT_PAGE_START=0 BUILDOUT_PAGE_END=9 BUILDOUT_CACHE_ONLY=1 BUILDOUT_PAGE_JITTER_MS=1500,3500 npx tsx collect.ts --source=lee-associates --transaction=sale --max-items=1 --concurrency=1 --out=/tmp/lee_window_should_not_ingest.json
BUILDOUT_ASSEMBLE_FROM_CACHE=1 npx tsx collect.ts --source=lee-associates --transaction=both --max-items=0 --concurrency=1 --out=out/lee_full_resumable_2026-06-12.json
python3 cre_ingest.py --in out/lee_full_resumable_2026-06-12.json --dry-run --keep-artifacts /tmp/lee_full_resumable_2026-06-12_ingest_check
```

The cache-only command should be designed not to live ingest and not to write a
partial production artifact. The final assembly command should fail closed until
cache coverage is complete.
