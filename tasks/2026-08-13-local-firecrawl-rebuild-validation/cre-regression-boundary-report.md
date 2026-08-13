# CRE listing regression boundary review

**Reviewed:** 2026-08-13
**Scope:** AGENTIC-2253 and its child issues, the local Firecrawl fork's custom operations layer, and protection of the CRE listing collector. This is a read-only review: no broker site, Supabase, PostgreSQL, scheduler, collector, or local runtime configuration was changed.

## Decision

The intelligent path is to keep the upstream Firecrawl API, SDK, and CLI as the product surface, and make only small fork-local *agent ergonomics* changes inside `scripts/firecrawl-ops/`. Do not turn the CRE collector into a user of a new CLI wrapper, generic RSS transformer, generic map-first flow, or search fallback. It has explicit source-level contracts and already calls the local API through its own SDK boundary.

The local-stack repairs currently in the worktree are outside the CRE source, ingest, SQL, and compose resource-profile paths. They may be accepted only after the focused regression gates below pass against the rebuilt API.

## Protected boundary

| Surface | Current contract | Change rule |
| --- | --- | --- |
| `cre_collector/lib/scrape.ts` | Sole shared Firecrawl SDK boundary for collector reads. It requests `markdown`, `links`, `images`, `rawHtml`, and attribute selectors; strict refreshes pass `maxAge: 0`; request deadlines and retries are local to the collector. | Do not replace it with CLI, MCP, the proposed agent helper, `/v2/crawl`, `/v2/map`, or `/v2/search`. Preserve every requested format and the `data ?? response` compatibility guard. |
| `cre_collector/sources/*.ts` | Source contracts mix direct provider APIs with Firecrawl reads. The source guidance identifies Firecrawl-heavy and direct-only sources explicitly. | No generic source-routing change. Direct enumeration remains authoritative where it is today; a Firecrawl fallback remains source-specific and validated. |
| `cre_collector/cre_daily_update.sh` | Treats `firecrawl_healthcheck.sh` exit status as a preflight gate before collection and ingest. | Healthcheck changes must preserve fail-closed nonzero behavior and must never expose `.env` values. Do not run the daily script as a regression test. |
| `set_cre_resource_profile.sh` + `docker-compose.yaml` | A reversible four-key resource profile constrains the API and Playwright service for supervised CRE refreshes. | Do not alter its allowlist, defaults, or restore semantics while fixing general compose env hygiene or queue capacity. A compose recreate is an explicit operator action. |
| `om_parse.py` / `cre_enrich.py` | Firecrawl's old OM/PDF writer is deliberately retired; GetCREdata is the only production OM-extraction writer. `--apply` fails closed. | A `/v2/parse` improvement may improve read-only diagnostics, but must not reactivate a write path, DDL, cache refresh, or job. |
| `cre_ingest.py`, SQL, Supabase/PostgreSQL, launchd | Governed listing persistence and scheduling. | Out of scope for AGENTIC-2253. No test or helper may acquire these credentials, query production, or write any row. |

The fork-only operations layer is broad: `.agents/skills/`, `docs/firecrawl-ops/`, and `scripts/firecrawl-ops/` are all additions relative to `upstream/main`. Within it, the high-risk CRE subtree is `scripts/firecrawl-ops/cre_collector/`, the legacy `cre_scrapers/`, the CRE SQL migrations, `cre_pipeline.py`, `set_cre_resource_profile.sh`, and the launchd templates. None of those surfaces was changed in the in-progress local API repairs at the time of this review.

## AGENTIC-2253 assessment

| Issue | Does it remain relevant? | Safest resolution | CRE impact / guardrail |
| --- | --- | --- | --- |
| AGENTIC-2254, crawl agent safety | Yes. The issue's local evaluation shows the upstream CLI does not accept crawl `--json` and its status view can lag the API. | Add a narrow helper operation that submits with the official HTTP API and polls `GET /v2/crawl/:id`. Keep upstream CLI available, but document that local agents must not use `crawl --wait`. | The listing collector does not use CLI crawl. Do not substitute crawl for source-specific enumeration. |
| AGENTIC-2255, CLI/HTTP envelopes | Yes for agent UX. | Normalize only at the new helper's output boundary, with an explicit raw option. Do not change API responses, SDK objects, or collector parsing. | `scrapeDoc()` already accepts either `data` or the inner object. Preserve that behavior. |
| AGENTIC-2256 and AGENTIC-2262, parse structure and `maxPages` | Needs a focused current repro. The reported `numPages: 8` with 37-page content is a real high-priority contract failure if reproduced. It is not evidence that listing ingestion is currently wrong. | Trace and fix the upstream API parser's page-limit propagation; test semantic output/page boundaries, not only metadata. Do not paper over it with a general `pdftotext` replacement or change default OCR profile. | The active listing collector does not use `/v2/parse`; the old Firecrawl OM writer fails closed. Use a public non-CRE fixture for the repro and retain the writer-retirement tests. |
| AGENTIC-2257, RSS/XML | Yes as a tool-selection problem, not necessarily an API defect. | Content-type preflight in the local helper: return a typed "use direct feed client" outcome, or implement a separately named feed reader. Do not reinterpret raw XML globally as markdown. | No CRE source is allowed to silently switch to a generic feed path. Keep source-specific parsers and identity/coverage gates. |
| AGENTIC-2258, search backend | Yes for reproducibility documentation; DDG fallback is acceptable discovery only. | Make backend provenance explicit and test the configured path. Do not purchase or enable Fire Engine without a founder decision. | Search must never be a strict inventory or freshness source. Existing CRE sources use direct provider enumerations or their own validated paths. |
| AGENTIC-2259, JS hubs | Yes as a usage recipe. | Document map-first discovery followed by targeted scrape for non-CRE agent workflows. Do not embed a generic map-first policy in the collector. | Sitemap/API enumeration and source-local coverage contracts remain controlling. |
| AGENTIC-2260, metrics-only helper | Yes; it should be small. | Extend the existing stdlib `firecrawl_request.py` rather than create a second client: crawl submit/poll, explicit envelope unwrap, optional User-Agent, metrics-only saved evidence. | No database settings, CRE imports, broker defaults, or automatic source selection. Default output must avoid storing source bodies in Git. |
| AGENTIC-2261, compose env hygiene and capacity | Yes, but low-risk hygiene must stay low-risk. | Add safe empty defaults for optional variables and report the effective queue limit. Preserve all resource-profile and user-supplied values. | Do not globally change `NUM_WORKERS_PER_QUEUE`, `CRAWL_CONCURRENT_REQUESTS`, `MAX_CONCURRENT_JOBS`, Playwright caps, service names, ports, volumes, or queue backend as part of warning cleanup. |

## Validation performed

All tests below are offline/pure-transform except the final neutral API probe. No commercial site, protected data system, or CRE write route was contacted.

| Check | Result | Evidence |
| --- | --- | --- |
| CRE resource-profile contract | Pass, 3 tests | `python3 scripts/firecrawl-ops/tests/test_cre_resource_profile.py` proves reversible allowlisted env changes, compose cap wiring, and no secret leakage. |
| Collector TypeScript typecheck | Pass | `npm run typecheck` in `scripts/firecrawl-ops/cre_collector`. |
| Collector TypeScript unit suite | Pass, 768 tests | `npm run test:unit` in `scripts/firecrawl-ops/cre_collector`; includes `lib/scrape.ts`, source adapters, coverage and identity guards. |
| Neutral API probe shaped like collector `scrapeDoc()` | Pass | Local `POST /v2/scrape` on `https://example.com` with collector formats returned HTTP 200 with non-empty markdown/raw HTML, one link, and array-shaped images/attributes. No body was retained. |
| Full Python collector suite | Blocked by one existing test-fixture defect | `2084 passed, 1 failed`: `test_cre_repair_cushman_identity.py::test_reviewed_artifact_loads_and_has_exact_geometry` requires the ignored, untracked reviewed artifact `out/checkpoint-refresh/2026-07-30T082113Z/sources/cushman-wakefield.json`. |

The failed test is not caused by the local API changes. The collector's `.gitignore` excludes `out/`, while the test unconditionally reads an artifact from that directory. This makes a claimed offline suite depend on machine-local historical data. Do not synthesize, download, or commit the reviewed Cushman artifact: it is an identity-repair input with a fixed hash and should remain controlled. The correct remediation is to make that test conditional on the reviewed artifact being provisioned, while retaining a tracked, synthetic unit contract for loader validation. The exact-artifact integrity check should run as an explicit owner-side preflight where the artifact is authorized and present.

## Required regression gates before integrating AGENTIC-2253 work

1. Keep the diff out of `cre_collector/`, `cre_scrapers/`, CRE SQL, `cre_pipeline.py`, and launchd unless a separately reviewed CRE change is needed. If `docker-compose.yaml` changes, review the four resource-profile keys and service resource limits line-by-line.
2. Rebuild/recreate only the required local API service, then run `firecrawl_healthcheck.sh` and the neutral collector-shaped scrape probe above. Confirm the healthcheck still fails closed when the API is unavailable.
3. Run `python3 scripts/firecrawl-ops/tests/test_cre_resource_profile.py`, `npm run typecheck`, and `npm run test:unit` from `cre_collector`.
4. Repair the missing-artifact test boundary, then require `python3 -m pytest tests -q` to pass completely. Until then, report it as a non-hermetic known failure, never as a green CRE suite.
5. Keep the existing OM-writer fail-closed tests in the Python run. Do not run `cre_daily_update.sh`, `collect.ts`, `cre_ingest.py --apply`, schema apply, `cre_validate.py`, or a live broker probe for this work.
6. For the PDF fix, use a public non-CRE document and assert that requested page limits constrain actual returned content, not merely `metadata.numPages`.

## Blockers and explicit non-actions

- There is no authorization or need to run a real CRE listing refresh, scheduler preflight, production database query, Supabase call, or protected commercial-platform scrape. Those would not be a safe regression probe.
- The missing Cushman artifact blocks a fully green Python collector suite. Its absence is a test-hermeticity defect, not permission to reconstruct a controlled repair artifact.
- General agent tooling should remain a separate local layer over upstream API/CLI. It must not become a second collector architecture.
