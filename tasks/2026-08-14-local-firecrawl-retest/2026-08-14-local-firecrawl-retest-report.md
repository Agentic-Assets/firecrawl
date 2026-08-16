# Local Firecrawl robustness retest

Date: 2026-08-14

## Result

The rebuilt local stack is healthy and the non-AI core remains operational.
Twelve of thirteen live smoke probes passed, the parser and wrapper suites
passed, and async crawl and batch jobs drained cleanly. This retest also found
three P2 diagnostic/transport defects and one external-provider degradation.
No source, Compose, environment, model, service, or collector configuration
was changed.

This report supersedes the runtime conclusions in the 2026-08-13 validation
set for the tested current checkout. It is not an approval to enable a model,
browser, agent, OCR, or CRE workflow.

## Scope and proof

- Checkout: `main` at `16fd0c90c66ffbfaf12b24a37f7d8fd225458bf8` before this
  report branch was created.
- Runtime: OrbStack Compose API, Playwright, Redis, RabbitMQ, FoundationDB,
  and NuQ Postgres were up; RabbitMQ and NuQ Postgres were healthy.
- Healthcheck passed API-root and scrape checks. Final queue state was
  `jobsInQueue=0`, `activeJobsInQueue=0`, `waitingJobsInQueue=0`, and
  `maxConcurrency=2`.
- Four independent read-only test lanes covered runtime/core routes, async
  jobs, parser/CLI/helper contracts, and invalid/configuration-gated paths.
- Generated live artifacts are ignored under
  `tasks/tmp/firecrawl-retest-2026-08-14/`. Temporary lane artifacts were
  retained outside the repository under `/tmp` during testing.

## Live results

| Surface | Result | Evidence / qualification |
| --- | --- | --- |
| Root, scrape, map, queue status, active crawls | Pass | HTTP 200; benign `example.com` scrape returned 180 Markdown characters and one link. |
| Search | Degraded | HTTP 200 returned zero results while API logs recorded DuckDuckGo anti-bot blocking after retries. The response shape is valid, but current URL discovery is unavailable from the configured fallback. |
| PDF parse | Pass | `fast` and `auto` passed the one-page PDF canary, each returning 415 Markdown characters. PDF, DOCX, and HTML helper paths also passed with compact `--metrics-only` outputs. |
| Batch scrape and crawl | Pass | Both reached `completed` under bounded polling, surfaced in active/queue state while running, and drained to zero. |
| Optional service gates | Mostly pass | Browser creation, agent creation, and support proxy correctly returned explicit 503 configuration gates. Browser session listing did not. |
| Structured extract | Configuration blocked | V2 submitted but reached an explicit failed state and V1 returned an explicit provider authentication error. Logs show Vercel AI Gateway 401 for the configured model; no provider key was changed. |
| LLMs.txt | Partial pass | The job completed and returned output, but per-page model description calls logged provider authentication failures. This is not evidence that AI-backed generation is ready. |

The full smoke matrix with mutating optional probes recorded 12 passes and one
failure. Its raw response artifacts intentionally remain ignored because they
can contain scraped page content.

## Local tests passed

- `scripts/firecrawl-ops/firecrawl_healthcheck.sh --evidence-dir ...`
- `scripts/firecrawl-ops/local_api_smoke_matrix.py ... --include-mutating-optional-probes`
  - 12 pass, 1 fail (browser listing defect below).
- `scripts/firecrawl-ops/pdf_parse_canary.py --pdf apps/test-site/public/example.pdf --modes fast,auto --max-pages 2 ...`
- Focused Python wrapper tests: 67 tests plus 38 subtests passed, covering
  helper, CLI, healthcheck, MCP, smoke-matrix, and capability-matrix contracts.
- `docker compose config --quiet`
- Direct bounded async tests for batch scrape, crawl, V2 extract, V1 extract,
  LLMs.txt, terminal-status behavior, and queue cleanup.
- Direct invalid-payload tests for scrape URL/formats and PDF `maxPages`
  bounds. These returned explicit HTTP 400 responses. The helper also rejects
  non-positive page caps before a request.

## Findings requiring follow-up

| Priority | Finding | Reproduction and impact | Recommended repair |
| --- | --- | --- | --- |
| P2 | Browser listing masks the missing browser service | `GET /v2/browser` returns opaque HTTP 500 when `BROWSER_SERVICE_URL` is absent, while `POST /v2/browser` returns the correct explicit 503. API logs identify an unconfigured database client. | Add the same prerequisite guard used by browser creation, returning a stable 503 without attempting to list sessions. |
| P2 | Unknown V2 extract IDs return an opaque 500 in self-hosted no-DB mode | `GET /v2/extract/00000000-0000-4000-8000-000000000000` dereferences a null request and returns `UNKNOWN_ERROR`; crawl, batch, and LLMs.txt equivalents return 404. | In `extract-status.ts`, return a clear 404 before the fallback references `created_at` on a missing request. |
| P2 | Helper transport timeouts leak a traceback and do not preserve an error artifact | A deliberately tiny `--timeout` exits 1 with a Python `TimeoutError` traceback; unlike HTTP errors, an `--out` target is not written. | Catch `TimeoutError`/transport `OSError`, emit a short diagnostic, and write a compact source-free transport-error record when output was requested. |
| P2 | Search has a false-healthy availability signal | `/v2/search` returns success with zero results when DuckDuckGo HTML is blocked. This differs materially from the prior report's two-result outcome. | Expose provider failure/degradation in the response or monitoring metric, and avoid treating `HTTP 200 + 0 results` as functional search availability. |

## Intentional gates and unverified areas

- AI-backed extraction, summary, JSON/query formats, and parameter preview are
  blocked until valid model-provider credentials are supplied and verified.
- Browser sessions and agent execution remain intentionally unavailable without
  `BROWSER_SERVICE_URL` and `EXTRACT_V3_BETA_URL`, respectively.
- OCR/Docling was not started or reconfigured. This retest does not establish
  quality for scanned, dense, table-heavy, or multi-column PDFs.
- The persistent parse warning that the selected engine does not support
  `skipTlsVerification` was nonfatal in the benign fixture; reconcile it only
  if that flag is required by an intended workflow.

## Next decision

Treat the stack as ready for the tested non-AI scrape, map, parse, batch, and
bounded-crawl workflows, with search availability monitored separately. Repair
the three P2 error-contract defects before calling the local API broadly robust.
Enabling any AI or optional browser/agent surface remains a separate
configuration and cost-approval decision.
