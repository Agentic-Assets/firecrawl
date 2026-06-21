# services/worker/ -- scrape/crawl job workers

Implements the queue consumers and job execution logic. The root CLAUDE.md describes the queue-driven architecture; this file covers the internals.

## Queue backends

Two queues run in parallel:

| Queue | Module | Notes |
|-------|--------|-------|
| **NuQ** (Postgres) | `nuq.ts` | Primary queue. `NuQJob<Data, ReturnValue>`. `scrapeQueue` is the default instance. `crawlGroup` groups crawl child jobs. `normalizeOwnerId` normalizes team IDs to UUIDs. |
| **BullMQ** (Redis) | via `queue-jobs.ts` (in services/) | Legacy queue still active. `addScrapeJob` / `_addScrapeJobToBullMQ` abstract both backends. Prefer `addScrapeJob` over calling BullMQ directly. |

## Job execution

`processJobInternal(job, logger)` in `scrape-worker.ts` -- canonical entrypoint for executing a scrape or crawl step.

Responsibilities: billing pre-check, semaphore acquire, scrape pipeline, crawl fan-out (via `crawl-logic.ts`), webhook dispatch, billing confirm, logging.

`crawl-logic.ts` -- extracted crawl fan-out logic: discovers next URLs, locks them, enqueues child scrape jobs.

## Concurrency (team-semaphore.ts)

Redis-backed semaphore keyed by `teamId`. Limits how many jobs a team can run simultaneously.

- `teamConcurrencySemaphore.acquire(teamId, holderId, limit)` -- must be called before `processJobInternal`
- `teamConcurrencySemaphore.release(teamId, holderId)` -- call in finally block
- TTL is 30s per holder; the reconciler worker cleans expired holders

## Worker processes

| File | Role |
|------|------|
| `nuq-worker.ts` | Main polling loop; dequeues and calls `processJobInternal` |
| `nuq-reconciler-worker.ts` | Reclaims stale/expired NuQ jobs |
| `nuq-prefetch-worker.ts` | Pre-warms the NuQ connection pool |

## Redis

`nuqRedis` from `redis.ts` -- separate Redis connection dedicated to NuQ semaphore operations (Lua scripts for atomic acquire/release). Do not use the main app Redis connection here.
