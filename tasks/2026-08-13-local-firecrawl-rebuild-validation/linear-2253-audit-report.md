# AGENTIC-2253 Local Firecrawl Audit

Date: 2026-08-13
Scope: live Linear issue `AGENTIC-2253`, all listed child issues, the current
local checkout, and existing local-only validation evidence. This audit did
not change source, Docker, `.env`, services, queue state, the CRE collector,
SQL, or EQUIRE/Supabase data.

## Bottom line

The upstream API is healthy for its core self-hosted surface. The real work is
small and should stay at the seam between upstream Firecrawl and this fork's
local-operator tools:

1. Fix the confirmed upstream PDF fallback `maxPages` defect.
2. Extend the existing dependency-free HTTP helper rather than introducing a
   second local client or changing the official CLI's output contract.
3. Make endpoint selection explicit: HTTP/XML for feeds, `map` for hubs, and
   search for discovery only.
4. Improve local diagnostics and compose defaults without changing capacity or
   any CRE collection path.

The current checkout is `a925132eb53269c89b12a7a4a90b9e0f0ea01ef2`; it is
based on the already-merged upstream v2.11.196 sync. `upstream/main` is now at
`8373dab922de2faf666e2c7ce3c6c3a8076b83b3`, but the three later upstream
commits do not supply the agent wrapper or the PDF fallback repair described
below. Re-running a broad upstream sync is not the correct fix for this work.

The rebuilt local stack's current evidence is green for the nine core probes:
scrape, map, search, fast-PDF parse, batch scrape and poll, crawl and poll,
queue status, and active-crawl visibility. See
`api-validation-report.md` and its referenced raw task artifacts. Browser,
LLM/extract, agent execution, and OCR are configuration-gated rather than
core-stack failures.

## Live Linear intake

`AGENTIC-2253` is a priority-2 Backlog issue in **Firecrawl Ops & Automation**.
Its two comments enumerate the child ordering and add `AGENTIC-2262`. Its
relations are `AGENTIC-188`, `AGENTIC-195`, `AGENTIC-196`, and `AGENTIC-2218`.
All children reviewed below remain Backlog and unassigned at read time.

The issue correctly excludes IRE.IQ/Salesforce writes, replacing EDGAR XML
with a scrape, cloud execution, browser automation, and Firecrawl cloud
credits. Those exclusions should remain in force.

## Claim-by-claim disposition

| Linear issue | Current disposition | Evidence | Minimal resolution |
|---|---|---|---|
| Parent: agent availability | Configuration gate, with a local diagnostic improvement already staged in the current working tree. | `apps/api/src/controllers/v2/agent.ts` now returns an explicit 503 when `EXTRACT_V3_BETA_URL` is absent. The latest validated container predates that source edit, so rebuild and targeted proof are still required. | Keep agent execution off until the upstream agent backend is deliberately configured. Retain and verify the explanatory 503; do not emulate the unavailable service. |
| `AGENTIC-2254` crawl JSON/status/wait | Local wrapper ergonomics gap; API contract itself works. | `firecrawl_cli.sh` is an exec-through wrapper at lines 109-109. Current `firecrawl-cli@1.20.0` exposes `--status` and `--wait`, but not `crawl --json`. The current local smoke already submits then polls `GET /v2/crawl/:id` successfully. | Add submit/status/bounded-poll support to the existing HTTP helper. Keep the official CLI raw rather than relying on its wait loop. Only route wrapper crawl commands after exact CLI compatibility tests cover every supported flag. |
| `AGENTIC-2255` CLI/API envelope | Local usability mismatch, not an upstream API defect. | HTTP responses use the API envelope; CLI scrape output intentionally writes the payload. `firecrawl_request.py:201-204` already has `response_payload()` for direct API results, but its CLI has no reusable normalization operation. | Make the helper's two modes explicit: API envelope by default and a deliberate payload-unwrapping mode. Document that raw official CLI output is CLI-shaped. Do not change Firecrawl CLI JSON globally. |
| `AGENTIC-2256` PDF/DOCX headings/list quality | Valid quality concern, but no safe generic rendering fix has been demonstrated. | Rust is the current upstream baseline for eligible text PDFs; the document engine calls upstream AnyDoc through `convertDocumentToMarkdown` at `engines/document/index.ts:80-84`. The latest basic parser canary does not test headings or numbered lists. | Add a non-sensitive fixture-based quality canary first. Use `fast` for eligible born-digital text, configure the existing Docling path only for layout/OCR needs, and use native `pdftotext` as a comparison oracle. Do not infer Markdown headings mechanically from uppercase text. |
| `AGENTIC-2257` RSS/XML | Wrong tool selection, not a reason to change generic `/v2/scrape`. | Firecrawl has an intentional raw XML/sitemap route, including `scraper/crawler/sitemap.ts:78-104`. Generic scrape is not a feed API and `rawHtml` is not a promise of wire-format RSS. | Put a firm routing rule in the local skill: native HTTP plus an RSS/Atom parser for feeds; Firecrawl `map`/scrape for HTML catalogs and articles. A future stdlib `feed` helper command is optional, not a prerequisite. |
| `AGENTIC-2258` search backend | Expected configuration choice plus an observability/documentation gap. | `search/v2/index.ts:42-84` selects Fire Engine, then SearxNG, then DuckDuckGo. `docker-compose.yaml:58-60` passes optional SearxNG values without empty defaults, producing misleading Compose warnings. The v2 response does not expose the provider. | Document this order and discovery-only semantics. Add a local diagnostic label only, and default known optional Compose variables to empty. Do not enable Fire Engine or add SearxNG without a separate cost/reproducibility decision. |
| `AGENTIC-2259` JS news hubs | Mostly already solved by existing upstream tools; a documentation gap remains. | Both Firecrawl skills say `POST /v2/map` is for discovery, and `crawl_swarm.py:114-116` maps each seed before scraping discovered URLs. | Add one explicit map-first rule for news/index hubs and preserve `waitFor` as a per-page exception. Do not add page-type heuristics to the API. |
| `AGENTIC-2260` agent helper | Partially present, still incomplete. | `firecrawl_request.py` is stdlib-only, supports direct `post`, saved fields, advanced parse options, and `response_payload()`, but the parser only registers `scrape`, `search`, `map`, `parse`, and `post` at lines 452-516. | Extend this one helper with `health`, `crawl` submit/poll, metrics-only output, an intentional unwrap option, bounded timeout/interval, and request-header input. Do not add the parallel `local_api.py` proposed in the issue. |
| `AGENTIC-2261` Compose/concurrency | Local clarity issue. The value 2 is a correct upstream fallback, not a Playwright setting. | `lib/concurrency-limit.ts:23-38` defines fallback `DEFAULT_CONCURRENCY_LIMIT = 2`; queue status exposes the effective value. Compose has several known optional variables with `${VAR}` interpolation and warning noise. | Document queue status as authority; add empty defaults only for optional variables and a static compose guard. Do not raise concurrency or alter CRE resource profiles in this batch. |
| `AGENTIC-2262` parse `maxPages` | Confirmed real API bug, currently present in upstream-derived code. | `pdf/index.ts:62-66` reads `maxPages`; lines 263-266 pass it to Rust and lines 459-505 pass it to FirePDF. The last fallback at lines 673-683 calls `scrapePDFWithParsePDF` without a cap. `pdfParse.ts:15` invokes `PdfParse` with no options. The installed library supports `{max}` and a local 25-page numeric-only probe rendered 25/1/2/8 pages respectively when passed max 0/1/2/8. | Pass `maxPages` to the fallback and call `PdfParse(buffer, { max: maxPages ?? 0 })`; add a later-page sentinel test and a local capped-PDF canary. This is safe to contribute upstream after local proof. |

## Related issues and protected boundaries

- `AGENTIC-188`: retain as the broader research-PDF validation packet. It
  should receive the fixture/quality evidence from `AGENTIC-2256`, not a
  speculative parser rewrite.
- `AGENTIC-195`: the existing map-first swarm is the appropriate upstream
  pattern. Do not start an autonomous collector until bounded HTTP crawl
  polling is implemented and independently proven.
- `AGENTIC-196`: `/v1/extract` auth/schema is a different, AI/configured path.
  It is not needed for the non-AI local API and must not be folded into this
  batch.
- `AGENTIC-2218`: its upstream sync is merged. The current CLI gap is a
  present-package contract issue, not a justification for another broad sync.

No proposed change requires a file below `scripts/firecrawl-ops/cre_collector/`,
the CRE SQL directory, source adapters, EQUIRE deployment code, or a database
write. The collector already uses source-specific public APIs/sitemaps where
appropriate; the general local API helper must remain an independent,
non-production operator tool.

## Recommended implementation order

1. **Repair `maxPages` first.** Add the two-file PDF fallback propagation and
   focused fixture test. Verify both a unit test and a bounded local parse
   whose returned body lacks a known later-page marker.
2. **Finish the single local helper.** Add health/crawl submit/status/poll,
   bounded timing, metrics-only output, and explicit envelope normalization to
   `firecrawl_request.py`. Test against mocked HTTP, then one `example.com`
   local crawl. Keep source bodies under ignored task output only.
3. **Simplify documentation and routing.** Teach the local skills: helper for
   agent automation, official CLI for its native workflows, SDKs for
   application code, map-first for hubs, native feed parsing for RSS/Atom, and
   search as discovery.
4. **Make optional configuration honest.** Apply only safe empty defaults,
   add a compose warning regression check, and print queue/concurrency from
   the existing queue-status endpoint. Do not touch resource limits.
5. **Prove isolation.** Run operations-helper tests, focused API tests/type
   check, rebuilt local smoke, and the selected read-only CRE regression gate.

## Recommended Linear update

Add one evidence comment to **AGENTIC-2253** while leaving its state,
assignee, routing labels, and Done status unchanged:

> Live audit complete on the local-only fork. Core rebuilt API evidence is
> green (scrape/map/search/parse/batch+crawl polling/queue). Confirmed one
> upstream-derived API defect: the final `pdf-parse` fallback ignores
> `maxPages` while metadata is capped (AGENTIC-2262). The remaining work is
> fork-local tooling/configuration: extend the existing HTTP helper for
> crawl-poll/metrics/envelope handling, document map-first/RSS/search routing,
> and silence intentional optional-env warnings without changing concurrency.
> Agent/browser/OCR remain deliberate configuration gates. No CRE collector,
> SQL, EQUIRE, or data-write path is in scope. Evidence:
> `tasks/2026-08-13-local-firecrawl-rebuild-validation/linear-2253-audit-report.md`.

As work lands, add the branch, commit, exact tests, local smoke artifact,
remaining configuration gates, and rollback note to that same issue. Do not
self-assign, change routing labels, or mark any issue Done without the owner
workflow.
