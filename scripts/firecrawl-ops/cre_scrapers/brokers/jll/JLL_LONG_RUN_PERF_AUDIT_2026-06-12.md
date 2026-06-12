# JLL Long Run Performance Audit - 2026-06-12

Scope: read-only audit of the active main `jll` collector run:

```bash
JLL_DETAIL_CONCURRENCY=6 npx tsx collect.ts --source=jll --transaction=both --max-items=0 --page-cap=100 --concurrency=6 --out=out/jll_full_detail_enriched_2026-06-12.json
```

No process was stopped, no competing main JLL job was started, no ingest was run,
and `collect.ts` was not modified.

## Context Read

- `scripts/firecrawl-ops/cre_collector/collect.ts`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/JLL_PERFORMANCE_REVIEW_2026-06-12.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/DETAIL_ENRICHMENT_SAFE_PATHS_2026-06-12.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/PERFORMANCE_ACCURACY_REVIEW_2026-06-12.md`
- Active log: `scripts/firecrawl-ops/cre_collector/out/jll_full_detail_enriched_2026-06-12.log`
- Active detail cache: `scripts/firecrawl-ops/cre_collector/out/cache/jll-detail/`

## Current Progress Estimate

Observed at 2026-06-12 16:25 CDT:

- Main collector process was still alive:
  - Node PID `83752`
  - elapsed about `01:21:37`
  - command matched the expected main `jll` full detail run
- Firecrawl queue status was healthy:
  - `jobsInQueue=0`
  - `activeJobsInQueue=0`
  - latest success timestamp was fresh at the time of sampling
- Docker stack was up:
  - `firecrawl-api-1`
  - `firecrawl-playwright-service-1`
  - `firecrawl-redis-1`
  - `firecrawl-rabbitmq-1` healthy
  - `firecrawl-nuq-postgres-1` healthy
- Final JSON output did not exist yet, which is expected while the run is still
  inside detail enrichment.
- Log search pagination had completed:
  - sale reached page 16 with 1,872 unique collected rows
  - lease reached page 87 with 9,358 unique collected rows
  - sale plus lease upper-bound row count is 11,230 before any cross-transaction
    URL overlap
- Detail cache state:
  - 8,167 cache JSON files at the final sample
  - cache directory was about 3.8 GB in the earlier sample
  - 2,281 files modified in the last 60 minutes
  - 1,138 files modified in the last 30 minutes
  - 570 files modified in the last 15 minutes
  - 191 files modified in the last 5 minutes
- A 30-second sample grew from 8,071 to 8,091 cache files, or about 40 files per
  minute during that window.
- A post-write verification sample saw the cache advance again to 8,167 files,
  which confirms the run was still moving while this note was being written.

Approximate progress:

- Using the sale plus lease upper bound, 8,167 cached detail files is about
  73 percent of 11,230.
- That estimate is imperfect because the cache may contain prior JLL detail
  pages and because sale and lease URLs can overlap, but it is the best live
  low-impact progress signal available before the final JSON is written.
- At the observed 38 to 40 detail files per minute, the upper-bound remaining
  uncached set of about 3,060 details would take roughly 75 to 85 more minutes.
  If sale and lease overlap is meaningful, the remaining time should be lower.

The active run appears healthy and still progressing. The log tail is quiet
because successful detail enrichments are not logged.

## Why It Is Slow

The bottleneck is not search pagination. Search finished and wrote clear rollup
lines to the log.

The slow phase is rendered detail enrichment:

1. `srcJll` collects public rendered search pages across nine property type
   filters for sale and lease.
2. It then calls `pmap(listings, JLL_DETAIL_CONCURRENCY, enrichJllListing)`.
3. `enrichJllListing` calls `scrapeJllDetailDoc` for each normalized public
   `property.jll.com/listings/...` URL.
4. Cache misses call Firecrawl `scrapeDoc` with:
   - `formats: ["rawHtml", "markdown", "links"]`
   - `waitFor: 8000`
   - `timeout: 120000`
5. The collector parses `#__NEXT_DATA__` for public property data, brokers,
   documents, images, coordinates, and stable JLL ids.

That gives high-quality rows, but it means thousands of browser-rendered detail
pages. At concurrency 6 and an 8-second wait per uncached detail page, the
runtime can still be measured in hours even when the stack is working.

## Error And Risk Signals

The current log showed two transient errors:

- one search `socket hang up` on `lease/office/page=1`
- one detail `socket hang up` for
  `stanford-pointe-plaza-6661-stanford-ranch-rd-not-tracked-california`

These are not fatal by themselves. `scrapeRaw` and `scrapeDoc` retry up to
three attempts, and `enrichJllListing` preserves the base row with `detailError`
if all detail attempts fail.

The main risk is raising load while JLL, local Firecrawl, or the browser
sidecar is already producing isolated socket failures. Higher concurrency may
increase:

- socket hang-ups
- empty or partially hydrated pages
- browser service pressure
- local memory pressure
- remote anti-bot throttling or challenge behavior
- row-local `detailError` rates

The code allows `JLL_DETAIL_CONCURRENCY` up to 10, but the live run is already
at 6. The observed socket failures argue against raising it during this run.

## Safe Speedup Options After This Run

Do not change the active run. After it finishes, these are the safer speedup
paths to test in bounded probes:

1. Preserve and reuse `out/cache/jll-detail/`.
   - This is already the most valuable speedup.
   - Restarts should keep the same cache directory unless testing cold-cache
     timing.
2. Add better progress logging for detail enrichment.
   - Log every 100 or 250 completed details with cache-hit and cache-miss
     counts.
   - This does not make the collector faster, but it prevents blind long runs.
3. Benchmark lower detail waits on a fixed sample.
   - Compare `waitFor` values such as 2000, 4000, and 8000 ms.
   - Require identical `#__NEXT_DATA__` field coverage before changing the
     production value.
4. Re-test a public GET-only Next data route only if derived from the current
   rendered page.
   - It must return the same public property payload as the rendered detail
     page.
   - Prior `_next/data` probes returned `notFound`, so this is not currently a
     proven replacement.
5. Consider split runs by transaction mode.
   - Sale and lease can be run separately into separate files, then reviewed
     independently.
   - This improves recoverability and makes progress easier to reason about,
     but it still needs the shared detail cache to avoid duplicate work.
6. Avoid speculative POST, GraphQL, gated deal-room, PDF download, or image
   download shortcuts.
   - Those are out of scope for this URL-only public collector unless a
     separate policy and implementation review approves them.

## Recommended Next Action

Let the current `JLL_DETAIL_CONCURRENCY=6` run continue.

If it keeps running but cache growth continues at the current rate, leave it
alone. It is doing the expensive work and should benefit future restarts.

If it keeps running too long and cache growth stalls:

1. Re-sample process status, queue status, log tail, and cache growth.
2. Treat it as stalled only if cache count does not move for 30 to 45 minutes
   and the Node process shows no meaningful CPU activity.
3. Do not stop the process without explicit approval.
4. If a restart is approved, restart with the same cache and lower
   `JLL_DETAIL_CONCURRENCY` to 4.
5. After completion, run dry-run ingest on the finished JSON before any live
   ingest.

Current recommendation: keep waiting, do not increase concurrency, and preserve
the detail cache.
