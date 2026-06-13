# lib/ -- shared API utilities

Canonical utility modules for the API layer. Root CLAUDE.md covers architecture and workflow; this file maps the modules.

## Key modules

| File | Purpose |
|------|---------|
| `logger.ts` | `logger` singleton (Winston). Always call `.child({ module, scrapeId, teamId })` at function start. |
| `withAuth.ts` | `RequestWithAuth<P,R,B>` type. `req.auth.team_id` and `req.acuc` (flags/plan). |
| `custom-error.ts` | `CustomError`, `TransportableError` (serializable across worker boundary). Prefer `TransportableError` for queue jobs. |
| `error.ts` | `ErrorCodes` enum (SCRAPE_FAILED, URL_BLOCKED, etc.). Import codes from here, not inline strings. |
| `format-utils.ts` | `hasFormatOfType(options, type)` for v2 `FormatObject` or string formats. `includesFormat` is the v1 string-only version. Use `hasFormatOfType` in v2+ code. |
| `crawl-redis.ts` | All crawl-state operations: `saveCrawl`, `addCrawlJob`, `lockURL`, `crawlToCrawler`, `getCrawl`. Do not read crawl state directly from Redis. |
| `concurrency-limit.ts` | `pushConcurrencyLimitActiveJob` / `concurrentJobDone` for per-team active job tracking. |
| `job-priority.ts` | `getJobPriority(teamId, acuc)`, `addJobPriority`, `deleteJobPriority`. Priority must be set before enqueue and deleted after job completes. |
| `cost-tracking.ts` | `CostTracking` accumulator, `CostLimitExceededError`. Pass a `CostTracking` instance into extraction pipelines. |
| `scrape-billing.ts` | `calculateCreditsToBeBilled(result)`. Credits are computed post-scrape, not pre. |
| `canonical-url.ts` | `normalizeUrl(url)`, `normalizeUrlOnlyHostname(url)`. Use for dedup keys. |
| `deployment.ts` | `isSelfHosted()`, `getErrorContactMessage()`. Gate cloud-only features on `isSelfHosted()`. |
| `generic-ai.ts` | `getModel(modelName)` returns a Vercel AI SDK `LanguageModel`. Always go through this, not provider SDKs directly. |
| `otel-tracer.ts` | `withSpan(name, fn)`, `setSpanAttributes(span, attrs)`. Wrap controller and worker entry points. |
| `permissions.ts` | `checkPermissions(body, flags)`. Returns `{ error }` when the team cannot use a requested feature. |

## Subdirectories

- `extract/` -- LLM extraction pipeline (see `extract/CLAUDE.md`)
- `branding/` -- branding profile extraction (entry: `branding/transformer.ts`)
- `deep-research/` -- deep-research orchestration helpers
- `generate-llmstxt/` -- LLM sitemap generation helpers
- `scrape-interact/` -- browser interaction primitives
- `deterministicJson/` -- deterministic JSON formatter
