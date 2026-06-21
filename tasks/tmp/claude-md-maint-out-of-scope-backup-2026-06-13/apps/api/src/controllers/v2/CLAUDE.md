# controllers/v2/ -- current production API

v2 is the canonical API version. v1 and v0 import types from here and convert via adapter functions (e.g., `fromV1ScrapeOptions` in v1/types.ts, `toV0CrawlerOptions` exported from here).

## Canonical types (types.ts -- 2200+ lines)

- `Document` -- the scrape output shape; all downstream systems accept this type
- `ScrapeRequest` / `scrapeRequestSchema` -- Zod-validated scrape request; parse with `scrapeRequestSchema.parse(req.body)` at controller entry
- `crawlerOptions` / `crawlRequestSchema` -- crawl parameters
- `scrapeOptions` -- subset of ScrapeRequest for per-page options inside crawl
- `RequestWithAuth<Params, ResBody, ReqBody>` -- Express request augmented with `req.auth` and `req.acuc`
- `URL` (exported Zod schema) -- http/https only, prepends protocol if missing
- `FormatObject` / `JsonFormatWithOptions` -- v2 format entries can be a string or an object; use `hasFormatOfType` from `lib/format-utils` to check

## Controller conventions

- All controller functions wrap the full handler body in `withSpan("api.<endpoint>.request", ...)` from `lib/otel-tracer`
- Job IDs are `uuidv7()` (time-ordered)
- Request body must be Zod-parsed inside the span before any business logic
- Auth data lives on `req.auth.team_id` and feature flags on `req.acuc?.flags`
- Errors surface as `TransportableError` (from `lib/custom-error`); do not throw plain strings
- Scrape jobs flow via `processJobInternal` (services/worker/scrape-worker) or enqueued via `addScrapeJob`

## New endpoints in v2 (not in v1/v0)

`monitor.ts`, `browser.ts`, `scrape-browser.ts`, `parse.ts`, `agent.ts`, `f-search.ts`, `support-proxy.ts`, `search-feedback.ts`
