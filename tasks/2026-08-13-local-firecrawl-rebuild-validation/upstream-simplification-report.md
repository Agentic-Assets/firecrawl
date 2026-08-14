# Upstream-first local API simplification review

Date: 2026-08-13
Scope: AGENTIC-2253 and children, local `http://localhost:3002`, and only the
fork-owned operations layer. This review did not modify the runtime, application
source, CRE collector, CRE SQL, or EQUIRE/Supabase data.

## Decision

Keep Firecrawl itself upstream-first. The effective local product should have
three deliberately small layers:

1. **Upstream API and SDKs:** use the v2 API as the contract. Application code
   should use the upstream JS/Python SDKs with `apiUrl` / `api_url` pointed at
   `http://localhost:3002`; they already implement bounded async crawl polling,
   pagination, and typed responses.
2. **Upstream CLI:** retain `firecrawl-cli` for interactive `scrape`, `map`,
   `search`, and `parse`. Do not attempt to make its output format an API
   contract or copy its command implementation into this fork.
3. **One fork-owned agent shim:** extend the existing
   `scripts/firecrawl-ops/firecrawl_request.py` rather than creating a second
   generic client. It is dependency-free, already owns API URL/auth,
   uploads/parser controls, artifact saving, and response unwrapping. Give it a
   crawl submit/poll command and explicit output modes for agents.

This preserves the useful local ergonomics without reimplementing upstream
Firecrawl or coupling the local API changes to commercial-real-estate
collection code.

## What the current repository already supplies

| Capability | Reuse it | Evidence |
| --- | --- | --- |
| Async crawl lifecycle | Yes, direct v2 API and upstream SDKs | `apps/api/src/controllers/v2/crawl.ts`, `apps/api/src/controllers/v2/crawl-status.ts`; `apps/python-sdk/example.py` and `apps/js-sdk/example_watcher.ts` show bounded polling/watching against configurable local URLs. |
| Shell access to local API | Yes, keep the existing thin wrappers | `scripts/firecrawl-ops/firecrawl_cli.sh` pins the official CLI to localhost; `firecrawl_request.py` owns direct HTTP and parser options. |
| DOCX/Office conversion | Yes, benchmark the new upstream engine before adding a local converter | `apps/api/src/scraper/scrapeURL/engines/document/index.ts` calls `convertDocumentToMarkdown`; that native binding uses upstream `anydoc` 0.1.6 in `apps/api/native/src/document.rs`. |
| Text PDF extraction | Yes, use the upstream Rust path first | `apps/api/src/scraper/scrapeURL/engines/pdf/index.ts` calls `processPdf(..., maxPages, ...)`; the bundled Rust binding explicitly limits extraction to the first N pages. |
| Hard/scanned PDF layout | Keep the fork's optional Docling FirePDF adapter | `scripts/firecrawl-ops/local_firepdf_ocr.sh` and `local_firepdf_ocr_service.py` add local-only OCR quality/back-pressure protection. This is a justified fork extension, not a replacement parser. |
| JS/news hubs | Yes, use upstream map then scrape selected articles | `/v2/map` is already available and the current helper exposes it in `firecrawl_request.py`. |
| Search fallback order | Yes, document the upstream behavior | `apps/api/src/search/v2/index.ts` selects Fire Engine, then SearxNG, then DuckDuckGo HTML. |
| Agent endpoint | Do not emulate it locally | `apps/api/src/controllers/v2/agent.ts` is an upstream remote-agent passthrough requiring `EXTRACT_V3_BETA_URL`; an explicit unavailable response is the right local behavior. |

The latest fetched upstream head was `8373dab922de2faf666e2c7ce3c6c3a8076b83b3`.
Its three commits beyond the fork do not repair crawl polling, document parsing,
search selection, or the page-limit fallback described below. It does remove
some fork-local OCR guardrail handling, so an upstream sync must keep the fork
OCR additions deliberately.

## AGENTIC-2253 status and disposition

| Issue | Current assessment | Intelligent treatment |
| --- | --- | --- |
| AGENTIC-2254, crawl CLI/polling | Still open. The official CLI resolved locally to 1.20.0 and rejects crawl `--json`; its `--wait`/status behavior is not a reliable local-agent waiter. The HTTP API itself is healthy and the smoke matrix already submits then polls `/v2/crawl/:id`. | Do not fork the CLI. Add `firecrawl_request.py crawl` with `submit`, `status`, and bounded `--wait` using HTTP. Optionally have `firecrawl_cli.sh` intercept only the crawl subcommand and delegate to it, preserving all other CLI behavior. Applications should use an upstream SDK waiter instead. |
| AGENTIC-2255, CLI/API envelope | Still a usability problem, but not an upstream API defect: CLI output files intentionally contain the payload while the HTTP API returns `{success,data}`. `response_payload()` already exists in `firecrawl_request.py`. | Make the local shim's modes explicit: API envelope by default, `--unwrap` for payload, `--metrics-only` for body-free output. Document that raw CLI output is CLI-shaped and never mix it with API response parsing. Do not alter official CLI JSON. |
| AGENTIC-2256, PDF/DOCX structure | Still needs a fixture-based quality decision. PDF headings are not safely recoverable by mechanically turning uppercase text into markdown headings. The new AnyDoc document engine is the correct upstream baseline for DOCX, but current tests only cover a simple sample document. | First add a non-sensitive fixture/canary that measures numbered-list markers and headings. Use Rust `fast` for eligible born-digital PDFs, Docling OCR only for scans/layout-sensitive documents, and native `pdftotext` only as an external comparison oracle. Change rendering only after the fixture proves a concrete regression. |
| AGENTIC-2262, parse `maxPages` | **Confirmed real API bug.** Rust and FirePDF paths receive the page cap, but the final fallback ignores it while metadata remains capped. This precisely matches the report of full text with `numPages: 8`. | Patch the fallback first. Pass `maxPages` from `pdf/index.ts` to `scrapePDFWithParsePDF`, then call installed `pdf-parse` with `{ max: maxPages }`. Add a test with a later-page sentinel so content and metadata agree. This is narrow, upstream-compatible, and should be proposed upstream after local proof. |
| AGENTIC-2257, RSS/XML | Still not a good Firecrawl scrape target, but not a parser failure to solve inside Firecrawl. Generic scrape correctly treats it as content; `rawHtml` is not a wire-format XML promise. | Document a firm routing rule: native HTTP/XML feed parser for RSS/Atom, Firecrawl map/scrape only for HTML catalog or article pages. A future optional `feed` subcommand may use Python stdlib XML, but do not change `/v2/scrape` semantics or add a second ingestion pipeline now. |
| AGENTIC-2258, search provenance | Fallback is already deterministic in code selection but external DDG results are inherently time-varying. Empty Fire Engine/SearxNG variables are expected in this local mode. | State the backend order, report the selected configured backend as a label in local diagnostics, and treat search as discovery only. Do not enable paid Fire Engine or add a SearxNG service without a separate founder cost/reproducibility decision. |
| AGENTIC-2259, JS hubs | The API behaves as expected: map finds URL inventory where rendered hub markdown is weak. | Make map-first the documented default. Keep `waitFor` as a per-page scrape option for exceptional hub inspection; do not add page-type heuristics to the API yet. |
| AGENTIC-2260, local agent helper | Partly addressed by the existing `firecrawl_request.py`; it already has stdlib HTTP, `response_payload`, direct `post`, secure headers, and parse controls. | Extend that file only: `crawl`, poll timeout/interval, metrics-only default for agent-oriented commands, `--unwrap`, and a descriptive User-Agent option. Avoid the proposed parallel `local_api.py` client. |
| AGENTIC-2261, compose/concurrency | Confirmed operator clarity gap. `docker-compose.yaml` has many optional variables without defaults, producing misleading blank-value warnings. The `maxConcurrency: 2` result comes from upstream `DEFAULT_CONCURRENCY_LIMIT` in `apps/api/src/lib/concurrency-limit.ts`, not from the Playwright worker count. | Add empty defaults only for known optional variables, plus a static compose-config test. Report queue/concurrency via existing `/v2/team/queue-status`; do not raise the default or alter CRE resource profiles in this work. |

## Confirmed parser root cause

`apps/api/src/scraper/scrapeURL/engines/pdf/index.ts` computes `maxPages`,
passes it to `processPdf`, and passes it to FirePDF/RunPod paths. Its final
fallback instead invokes:

```ts
scrapePDFWithParsePDF(meta, tempFilePath)
```

In `apps/api/src/scraper/scrapeURL/engines/pdf/pdfParse.ts`, that helper calls
`PdfParse(await readFile(tempFilePath))` with no options. The installed
`pdf-parse` library supports its `max` option. Consequently, a complex PDF
that falls back to `pdf-parse` returns all pages while `pdfMetadata.numPages`
still uses the capped `effectivePageCount`.

The appropriate patch is therefore a two-file parameter propagation change,
not a new PDF stack. It needs a unit test plus an end-to-end canary using a
public/non-sensitive PDF whose later-page marker can be checked.

## Recommended implementation sequence

1. **Repair and prove the page cap.** Patch the two PDF files, add focused
   fallback tests, then run the normal parser snip and a local bounded
   `maxPages` canary. Keep OCR and CRE files untouched.
2. **Finish one local shim.** Add crawl submit/status/bounded polling to
   `firecrawl_request.py`; retain its direct API response mode and make
   `--unwrap`/`--metrics-only` intentional. Test with mocked HTTP and one
   `example.com` local smoke. Route `firecrawl_cli.sh crawl` only if the
   wrapper can preserve documented CLI flags and exit codes exactly.
3. **Use upstream SDKs for application code.** Add one local SDK example and
   smoke for `api_url=http://localhost:3002`; this is the strongest fix for
   application-level polling and pagination, without a custom client.
4. **Document endpoint routing, not artificial conversion.** Add the
   map-first, XML/RSS, search-provenance, envelope, and PDF-mode selection
   rules to the two Firecrawl skills and `tools-capabilities.md`.
5. **Make configuration warnings honest.** Default only optional compose
   values to empty, add a compose warning regression test, and surface queue
   facts from `/v2/team/queue-status`. Do not change CPU, browser limits, or
   `maxConcurrency` defaults.
6. **Prove isolation.** Run the relevant operations tests, API typecheck and
   parser tests, local smoke matrix, and the CRE boundary/regression suite
   selected by the separate CRE audit. No collector, SQL, or data write is
   needed for these changes.

## Acceptance evidence

- `maxPages: 8` returns no known page-20 text, and actual returned pages agree
  with `metadata.numPages`.
- A local helper crawl reaches a terminal status through `/v2/crawl/:id` within
  its explicit timeout and reports a job id, terminal status, and metrics.
- The same scrape can be requested in documented API-envelope and payload-only
  shapes; metrics-only output contains no source body.
- RSS/Atom, search, and JS hub routing are explicit in the skill, with no
  source-body artifacts committed.
- `docker compose config` has no warnings for intentionally unset optional
  services; `/v2/team/queue-status` remains the concurrency authority.
- No changes under `scripts/firecrawl-ops/cre_collector/`,
  `scripts/firecrawl-ops/sql/`, CRE source adapters, or EQUIRE write paths.
