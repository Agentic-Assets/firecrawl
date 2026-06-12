# JLL Performance Review - 2026-06-12

Scope: source key `jll` in `scripts/firecrawl-ops/cre_collector/collect.ts`
while the full detail-enriched run is active. This review is read-only except
for this note. No process was stopped. No PDFs, images, gated pages, or private
paths were fetched. No secrets were printed or written.

## Sources Read

- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/DETAIL_ENRICHMENT_SAFE_PATHS_2026-06-12.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/jll/PERFORMANCE_ACCURACY_REVIEW_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/collect.ts`, JLL search, detail cache,
  and detail enrichment sections
- `scripts/firecrawl-ops/cre_collector/out/jll_full_detail_enriched_2026-06-12.log`

## Active Run Health

The active command is still running from `scripts/firecrawl-ops/cre_collector`:

```bash
JLL_DETAIL_CONCURRENCY=6 npx tsx collect.ts --source=jll --transaction=both --max-items=0 --page-cap=100 --concurrency=6 --out=out/jll_full_detail_enriched_2026-06-12.json
```

Observed at about 2026-06-12 15:48 America/Chicago:

- Process tree was alive: shell, `npm exec`, `tsx`, `tee`, and the Node collector
  were still present. The collector process was using CPU during the sample.
- Local stack was up: `api`, `playwright-service`, `redis`, `rabbitmq`, and
  `nuq-postgres` containers were running, with RabbitMQ and NuQ Postgres healthy.
- Firecrawl queue status returned success with `jobsInQueue=0`,
  `activeJobsInQueue=0`, and a fresh `mostRecentSuccess`.
- The log had 349 lines and had not appended during the short sample, but detail
  successes are silent in this collector.
- The JLL detail cache grew from 6,749 files to 6,774 files over a 20 second
  sample. That is the strongest signal that detail enrichment is still advancing.
- The cache directory was about 3.2 GB, and 384 cache files had been modified in
  the last 10 minutes.
- A later verification while writing this note saw 6,842 cache files and the
  same active process tree still alive.
- The final JSON output file did not exist yet, which is expected before the
  full collector completes and writes the run artifact.

The process appears healthy. The single visible latest log line is a detail
`socket hang up` on attempt 1 for one listing. That is not by itself fatal:
`scrapeDoc` retries up to 3 attempts, and `enrichJllListing` preserves the row
with `detailError` if all detail attempts fail. Search also had one earlier
socket hang-up and recovered.

## Current Coverage State

The search phase appears complete for the active run:

- Sale reached page 16 and collected 1,872 unique URLs.
- Lease reached page 87 and collected 9,358 unique URLs.
- `--page-cap=100` is the correct cap for this run because office lease needed
  page 87.

The run is now in the rendered detail enrichment phase. That phase is the
bottleneck because the current complete path renders one public
`property.jll.com/listings/...` detail page per normalized listing URL and
parses public `script#__NEXT_DATA__`.

## Speedup Review

No safe, repeatable speedup beyond the current `JLL_DETAIL_CONCURRENCY=6` plus
URL-keyed detail cache is ready to apply during this active run.

Why:

- The practical bottleneck is browser-rendered detail pages, not search parsing.
- The URL-keyed cache is already active and is doing useful work. Restarts will
  reuse completed rendered detail pages.
- `JLL_DETAIL_CONCURRENCY=6` is already above the conservative documented
  default and is currently making progress.
- The active run has produced isolated socket hang-ups at concurrency 6. That is
  tolerable with retries, but it argues against raising detail concurrency to 8
  or 10 without a separate bounded test.
- The code technically caps `JLL_DETAIL_CONCURRENCY` at 10, but the docs and
  current live evidence do not support using that ceiling for the full run.
- Candidate `_next/data` detail routes were previously tested from cached
  `buildId`, page, and query values and returned `{"notFound": true}`. That is
  not a proven public replacement for rendered detail pages.
- Direct GraphQL or PDP POST calls are not acceptable as a speculative speed
  path here. They are unproven, POST-based, and would require exact browser
  request capture and a separate policy review.
- Card-only collection is much faster, but it drops stable JLL property ids,
  coordinates, public documents, richer images, and broker contact/profile
  fields. It is not a substitute for the current detail-enriched run.
- Adding more live probes while this process is active would add load to local
  Firecrawl. The current goal is to let the long run finish, not compete with it.

## Safety Review

The current main JLL collector stays inside the intended public URL-only posture:

- Discovery uses public rendered search pages with `tenureTypes` and
  `propertyTypes` query parameters.
- Detail enrichment uses public listing URLs under `property.jll.com/listings/`.
- Assets are stored as URLs only from public `brochures`, `floorPlans`,
  `images`, public broker profile slugs, avatars, and related public fields.
- No PDFs or images are downloaded by the collector.
- Detail failures are row-local and do not require stopping the full run.

Do not add these as performance shortcuts:

- No gated deal-room paths.
- No binary document or image downloads.
- No speculative POST or GraphQL calls.
- No `jll-investor` pagination changes in this source key.
- No ingest from a partial artifact while the active run is still in progress.

## Recommended Next Action

Let the active `JLL_DETAIL_CONCURRENCY=6` run continue without changes.

If it finishes:

1. Review final log counts for detail failures and socket failures.
2. Dry-run ingest the completed JSON before any live ingest.
3. Preserve `out/cache/jll-detail/` for restart and future refresh reuse.

If socket failures become sustained or cache growth stalls for a long interval:

1. Do not stop the process without explicit approval.
2. Re-sample process status, Firecrawl queue status, and cache growth.
3. If a restart is later approved, restart with the same cache and lower
   `JLL_DETAIL_CONCURRENCY` to 4.

Only after the active run finishes, consider a bounded speed experiment:

- Use a temporary `JLL_DETAIL_CACHE_DIR` so cache hits do not hide timing.
- Compare a small set of public detail URLs at lower detail waits, such as
  2000, 4000, and the current 8000 ms, and require identical `__NEXT_DATA__`
  field coverage before changing production behavior.
- Re-test a public GET-only Next data route only if it is derived from the
  current rendered page and returns the same public property payload. Treat any
  POST, gated, or deal-room path as out of scope.

Until one of those bounded tests passes, the repeatable speed path remains:
current rendered-detail enrichment, `JLL_DETAIL_CONCURRENCY=6`, and the
URL-keyed cache.
