# Local Firecrawl Rebuild API Validation

Validated 2026-08-13 against `http://localhost:3002`. This was a direct, local API validation of the rebuilt OrbStack stack. No source, Docker, environment, or service configuration was changed by this validation.

## Result

Core non-AI API workflows passed. The smoke matrix completed 9 of 9 core probes with no failures. Direct probes confirmed the asynchronous batch and crawl contracts, including active-crawl and queue visibility. One P2 diagnostic-contract finding remains on the intentionally unavailable agent endpoint.

## Commands and evidence

All generated response artifacts are under `tasks/tmp/20260813-api-validation/`:

- `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh --evidence-dir tasks/tmp/20260813-api-validation/healthcheck-current`
  - Passed. Docker compose reported API, Playwright, Redis, RabbitMQ, FoundationDB, and NuQ Postgres running; NuQ Postgres and RabbitMQ were healthy.
  - Current evidence: `healthcheck-current/20260813-063803-firecrawl-healthcheck.json` and `.md`.
- `scripts/firecrawl-ops/local_api_smoke_matrix.py --api-url http://localhost:3002 --crawl-url https://example.com --batch-url https://example.com --search-query 'Firecrawl documentation' --parse-file apps/test-site/public/example.pdf --timeout 120 --poll-timeout 90 --poll-interval 1 --out-dir tasks/tmp/20260813-api-validation/smoke`
  - Passed 9 core probes, failed 0, skipped 4 optional mutating probes.
  - Evidence: `smoke/20260813-063350-local-api-smoke.json` and `.md`.
- Bounded direct `curl` probes against `https://example.com`, plus `apps/test-site/public/example.pdf` for local parse.
  - Raw responses: `direct/`.

Representative direct commands, with each response saved in that `direct/` directory, were:

```bash
curl -sS -X POST http://localhost:3002/v2/scrape -H 'Content-Type: application/json' --data-raw '{"url":"https://example.com","formats":["markdown","html","links"]}'
curl -sS -X POST http://localhost:3002/v2/map -H 'Content-Type: application/json' --data-raw '{"url":"https://example.com","limit":10}'
curl -sS -X POST http://localhost:3002/v2/search -H 'Content-Type: application/json' --data-raw '{"query":"Firecrawl documentation","limit":2}'
curl -sS -X POST http://localhost:3002/v2/parse -F 'options={"formats":["markdown"],"parsers":[{"type":"pdf","mode":"fast","maxPages":2}]}' -F 'file=@apps/test-site/public/example.pdf'
curl -sS -X POST http://localhost:3002/v2/batch/scrape -H 'Content-Type: application/json' --data-raw '{"urls":["https://example.com"],"formats":["markdown"]}'
curl -sS -X POST http://localhost:3002/v2/crawl -H 'Content-Type: application/json' --data-raw '{"url":"https://example.com","limit":1,"scrapeOptions":{"formats":["markdown"]}}'
curl -sS http://localhost:3002/v2/team/queue-status
curl -sS http://localhost:3002/v2/crawl/active
```

Batch and crawl status were polled by their returned job ID with bounded one-second intervals. Capability boundaries used `POST /v2/browser` with `{"ttl":30,"activityTtl":10}` and `POST /v2/agent` with a minimal public-URL payload.

## Core results

| Surface | Result | Evidence |
|---|---|---|
| API root | Pass, HTTP 200 | Firecrawl API identity returned |
| `POST /v2/scrape` | Pass, HTTP 200 | Markdown 180 chars, HTML 258 chars, 1 link, page title `Example Domain` |
| `POST /v2/map` | Pass, HTTP 200 | Valid link array; 0 same-site links is expected for the minimal example host |
| `POST /v2/search` | Pass, HTTP 200 | 2 search results for `Firecrawl documentation` |
| `POST /v2/parse` | Pass, HTTP 200 | Local `example.pdf`, fast parser, 415 markdown chars |
| `POST /v2/batch/scrape` then status | Pass, HTTP 200 | Submitted one public URL and reached `completed` after 16 bounded polls |
| `POST /v2/crawl` then status | Pass, HTTP 200 | Submitted one public URL and reached `completed`; active endpoint showed 1 crawl immediately after submission |
| `GET /v2/team/queue-status` | Pass, HTTP 200 | Queue drained after work: `jobsInQueue: 0`, `activeJobsInQueue: 0` |
| `GET /v2/crawl/active` | Pass, HTTP 200 | Returned a valid crawl list; empty after completion |

## Expected limitations and configuration gates

- Browser sessions are not configured locally. `POST /v2/browser` correctly returned HTTP 503 with `BROWSER_SERVICE_URL is missing`; no session was created. This is an expected configuration gate, not a core-stack defect.
- AI-backed summary, JSON extraction, query, parameters preview, and extract workflows were not invoked. Provider readiness and no-cost execution were not established, so these remain configuration gates rather than failures.
- OCR-specific PDF validation was not invoked. The tested fixture used the local fast parser; Docling and external OCR routing are outside this bounded non-AI validation.
- The smoke matrix deliberately skipped browser creation, agent creation, and support proxy probes because they can create work or call optional services. The browser and agent boundary checks below were narrowly targeted configuration responses only.

## Ranked defects

| Rank | Finding | Reproduction and likely root | Impact |
|---|---|---|---|
| P2 | Unconfigured `/v2/agent` has an opaque failure contract. | `POST /v2/agent` with a minimal prompt, public `https://example.com` URL, and `maxCredits: 1` returned HTTP 500 with `code: UNKNOWN_ERROR` and an opaque error ID. The controller throws `new Error("Agent beta is not enabled.")` when `EXTRACT_V3_BETA_URL` is absent at `apps/api/src/controllers/v2/agent.ts:128-130`; generic error handling masks the prerequisite. This also conflicts with `local_api_smoke_matrix.py`, whose optional probe expects the explanatory text. | Operators cannot distinguish an expected local configuration gate from a genuine agent failure, and the optional smoke probe would falsely fail if enabled. Prefer a stable explicit 503 response naming the missing prerequisite, analogous to `apps/api/src/controllers/v2/browser.ts:226-231`. |

No P0 or P1 defect was found in the tested non-AI scrape, map, search, parse, queue, batch, or crawl workflows.
