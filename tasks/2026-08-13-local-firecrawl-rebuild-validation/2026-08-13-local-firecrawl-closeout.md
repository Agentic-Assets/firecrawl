# Local Firecrawl rebuild closeout (2026-08-13)

**Branch:** `fix/local-firecrawl-rebuild-validation`
**Base:** `origin/main` at `a925132eb53269c89b12a7a4a90b9e0f0ea01ef2`
**Implementation commit:** `81a1f8f32df8d2a2a42a650e36bb47617453c0ae`
**State at final handoff:** draft [PR #30](https://github.com/Agentic-Assets/firecrawl/pull/30)
is open from this branch. Local verification is complete; GitHub CI has not
started a reported check yet.

## Goal

Rebuild and validate the local OrbStack Firecrawl stack, resolve confirmed
AGENTIC-2253 defects with minimal upstream-compatible changes, and preserve
the commercial-real-estate collection boundary.

## What shipped

- PDF `maxPages` propagation into the `pdf-parse` fallback, plus a public
  long-PDF regression test and local API proof.
- Clear 503 diagnostics for an unconfigured agent endpoint.
- One enhanced `firecrawl_request.py` agent helper: bounded crawl polling,
  predictable output, compact metrics, health visibility, safe page-cap
  validation, and saved terminal/timeout/error artifacts.
- Healthcheck evidence repair, current MCP JSONL handshake validation,
  Compose optional-variable defaults, capability-smoke freshness selection,
  and updated operations documentation/skills.
- Independent API, CLI/MCP, parser/ops, Linear, upstream-first, CRE boundary,
  and adversarial implementation-review reports in this directory.

## Verification

- `pnpm harness vitest run src/scraper/scrapeURL/engines/pdf/__tests__/pdfParse.test.ts src/controllers/v2/__tests__/agent.test.ts`: 2 passed.
- `pnpm run build` from `apps/api`: passed.
- Rebuilt-stack healthcheck: passed before and after the API harness.
- Latest local smoke matrix: 9 core passed, 0 failed, 4 expected optional
  skips. It covers root, scrape, map, search, fast PDF parse, batch polling,
  crawl polling, queue status, and active crawls.
- Ops suites: CLI wrapper 5, MCP wrapper 4, healthcheck 2, capability matrix
  1, Compose optional-env 1, direct-helper 30. `docker compose config --quiet`
  passed.
- CRE boundary: resource-profile tests 3, collector typecheck, and 768 unit
  tests passed. No CRE collector, SQL, source adapter, scheduler, data, or
  resource-profile file changed.

## Decisions

- Preserve upstream API, SDKs, and interactive CLI. Extend only the existing
  dependency-free helper for agent-specific output and bounded polling. A
  second client or a CLI rewrite would duplicate upstream contracts.
- Treat RSS/XML as a direct-feed-client use case, map as hub discovery, and
  search as discovery only. No generic parser or source-routing rewrite.
- Do not infer Markdown headings or replace document parsers without a
  representative, approved quality fixture.
- Keep the CRE collector isolated from CLI, MCP, map, crawl, and helper
  changes. The collector's source-level contracts remain authoritative.

## Deferred and operator gates

- AGENTIC-2256 needs a non-sensitive DOCX/PDF fixture and agreed quality
  criteria before changing structural extraction behavior.
- Browser, agent, AI extraction, and OCR are separately configured services;
  their unavailable responses are diagnostic, not a reason to enable or
  provision them in this branch.
- The full CRE Python suite remains blocked by one pre-existing missing ignored
  Cushman artifact (2084 passed, 1 failed). This closeout does not manufacture
  a substitute.
- Review the draft PR and merge only with required human approval. Roll back
  with `git revert 81a1f8f32` and rebuild the API service.

## Post-closeout hardening

- Commit `1f131f099f1a53de0bf59b7441e0343786f35b6a` repaired four verified
  `firecrawl_request.py` contracts: wall-clock-bounded crawl polling, finite
  polling and HTTP timeouts, redacted HTTP error bodies for `--metrics-only`,
  and JSON-object validation for `--headers-file` before User-Agent overlays.
- The EQUIRE `subagent-code-review` workflow independently reviewed the API,
  helper, and shell/ops deltas; verifiers retained only the helper issues.
  Versioned Vercel DeepSeek Gateway IDs were checked and intentionally kept.
- Post-commit verification: the local ops suite reported `98 passed, 1
  skipped, 38 subtests passed`; focused helper coverage was 93%; and a fresh
  2026-08-13 metrics-only health probe after that helper commit reported API
  and queue HTTP 200, an empty queue, and `maxConcurrency: 2`.
- Root agent context now follows the company `AGENTS.md` bridge policy: unique
  shared rules are in `AGENTS.md`; root `CLAUDE.md` is the required two-line
  pointer. Current CRE data and scheduler contracts remain in the scoped
  `scripts/firecrawl-ops/` guidance and were not changed.
