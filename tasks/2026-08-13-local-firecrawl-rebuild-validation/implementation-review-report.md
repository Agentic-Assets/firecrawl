# AGENTIC-2253 implementation review

Date: 2026-08-13

Scope: read-only adversarial review of the current working tree. I inspected
the PDF fallback, direct local HTTP helper, Compose defaults, affected tests,
and the changed-path boundary to CRE tooling. No runtime was started or
changed during this review.

## Verdict

**Do not merge the current helper changes yet.** The PDF fallback and Compose
changes are sound, but two helper edge cases make automation report success
when it should not, or silently ignore an invalid page limit. Both are narrow
to fix and neither requires changing upstream SDKs or CRE collectors.

## Confirmed findings

### P1: `parse --max-pages 0` silently removes the requested limit

- **Location:** `scripts/firecrawl-ops/firecrawl_request.py:427-433`,
  `:706-709`
- **Failure scenario:** An agent invokes `parse report.pdf --max-pages 0`.
  `argparse` accepts the integer, but each `if args.max_pages` check treats
  zero as absent. The helper therefore omits the PDF parser object entirely.
  The API applies its default parser and can parse the entire document rather
  than rejecting the invalid positive-only `maxPages` value.
- **Why confirmed:** The API contract at
  `apps/api/src/controllers/v2/types.ts:481-492` specifies a positive integer.
  The helper's zero-truthiness branches bypass that validation. This is
  especially risky on a long research/CRE PDF because it defeats an explicit
  workload cap.
- **Recommended repair:** Use a shared positive-integer argparse validator for
  `--max-pages`, then test `is not None` when building parser options. Add
  regressions for `0` and negative values, and retain the existing happy path.

### P1: non-waiting `crawl-status` exits zero for a failed crawl

- **Location:** `scripts/firecrawl-ops/firecrawl_request.py:614-619`
- **Failure scenario:** A local agent submits a crawl, later runs
  `crawl-status <id>` without `--wait`, and receives the API's successful HTTP
  response whose body is `{ "success": false, "status": "failed", ... }`.
  The helper delegates to `run_and_write`, which only checks the HTTP status,
  so it exits `0`. Shell automation consequently records the failed crawl as a
  successful command.
- **Why confirmed:** The v2 controller deliberately returns failed crawl state
  as HTTP 200 at `apps/api/src/controllers/v2/crawl-status.ts:256-273`; the
  `--wait` path already handles this correctly in `poll_crawl`.
- **Recommended repair:** Centralize terminal crawl-state handling and use it
  in both `crawl-status` modes. Write the response first, then exit non-zero
  for `failed` or `cancelled`. Add unit coverage for non-wait failure and
  cancellation, not only successful completion.

### P2: timeout/failure paths do not reliably create the requested artifact

- **Location:** `scripts/firecrawl-ops/firecrawl_request.py:575-611`,
  `:180-191`
- **Failure scenario:** `crawl --wait --out result.json` times out before a
  terminal response. The submit response and latest polling response are not
  written, so the caller gets only a text exception containing the crawl id.
  Also, real HTTP errors exit in `open_request` before the new command-level
  `status >= 400` branches can call `write_response`; the current test reaches
  those branches only by mocking an impossible return from `request_json`.
- **Why confirmed:** `open_request` raises on every `HTTPError`, while
  `cmd_health`, `cmd_crawl`, and `run_and_write` assume it can return a
  `>=400` status. A timeout in `poll_crawl` throws without emitting either
  response.
- **Recommended repair:** Preserve the established stderr behavior, but make
  the new polling workflow durable: write the accepted submit response before
  polling or write a compact timeout record containing the crawl id and last
  known state. If error responses must be saved, refactor `open_request` to
  return `(status, body)` for HTTP errors and update the pre-existing error
  contract/tests in one deliberate change. Do not claim the current
  `--out`/`--metrics-only` error behavior is covered until the real transport
  path is tested.

### P2: `health --metrics-only` drops the endpoint status facts it computes

- **Location:** `scripts/firecrawl-ops/firecrawl_request.py:218-253`,
  `:526-539`
- **Failure scenario:** The health command builds a body-free summary with
  both `apiHttpStatus` and `queueHttpStatus`, then hands it to
  `write_response`. With `--metrics-only`, `write_response` runs that summary
  through `response_metrics` again. That whitelist omits both fields, leaving
  only a generic `httpStatus` for the root request.
- **Why confirmed:** The current health test patches `write_response` and
  therefore asserts the pre-normalization object rather than the emitted
  output. An in-memory command invocation reproduces the omission.
- **Recommended repair:** Treat the health summary as already compact and
  bypass metrics normalization for it, or explicitly preserve both endpoint
  status fields in `response_metrics`. Assert the actual emitted JSON.

## Confirmed safe changes

- **PDF fallback:** The call site in
  `apps/api/src/scraper/scrapeURL/engines/pdf/index.ts:675-684` now forwards
  `maxPages`. `pdf-parse@1.1.1` accepts `{ max }` and caps its render loop, so
  `pdfParse.ts:15-21` is the correct upstream-compatible adapter. The API
  schema accepts only positive `maxPages`, making the fallback's truthiness
  guard safe on API-originated calls. The new fixture test exercises the
  fallback's output cap. No upstream SDK/API contract is changed.
- **Compose optional defaults:** The substitutions in `docker-compose.yaml`
  preserve a set variable, provide an empty value for an omitted optional
  integration, and leave core Redis/Postgres/queue/PDF defaults intact. The
  config schema maps an empty `NUQ_BACKEND` to undefined, so the change does
  not switch queue backends. The static test covers the listed variables.
- **CRE isolation:** No changed source path is in `cre_scrapers`,
  `cre_collector`, or the governed CRE contract. The helper remains a thin
  local wrapper; its existing CRE callers retain their scrape payload path.
  The two findings above are generic helper semantics and can be fixed without
  altering any CRE source contract, broker workflow, or data write path.

## Refuted candidates

- Passing `{ max: maxPages }` to `pdf-parse` is not an unsupported API. The
  installed `pdf-parse@1.1.1` documents `max` and caps the page loop with it.
- `maxPages ? { max: maxPages } : undefined` in the TypeScript fallback does
  not itself permit a zero-page request, because the public v2 schema rejects
  non-positive values before it reaches this path.
- The Compose empty-default conversion does not enable AI, OCR, Supabase, or
  FoundationDB. Those integrations remain opt-in through explicit variables.

## Minimal resolution order

1. Validate helper numeric/poll arguments and correct terminal crawl exit
   semantics, with focused unit tests.
2. Make bounded crawl timeout output durable without changing the upstream API
   or CLI.
3. Re-run the helper suite, PDF fallback test, Compose optional-env test, and
   a local bounded crawl canary. Then rerun the CRE boundary tests separately.
