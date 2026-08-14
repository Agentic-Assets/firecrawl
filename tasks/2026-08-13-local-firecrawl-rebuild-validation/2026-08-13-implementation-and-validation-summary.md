# Local Firecrawl rebuild implementation and validation summary

Date: 2026-08-13

## Decision

Keep the product upstream-first. Applications should use the upstream API and
SDKs pointed at the local API URL, the upstream CLI remains the interactive
tool, and this fork retains one small dependency-free helper for local agents.
The work does not introduce a second client, alter the API envelope, rewrite
the official CLI, or change a commercial-real-estate collector contract.

## Implemented

- Rebuilt the local OrbStack Compose stack and verified the API, queue, and
  sidecars after the rebuild.
- Fixed the upstream-compatible PDF fallback: `maxPages` now reaches
  `pdf-parse`, with a public 816-page fixture proving a one-page result has
  `numPages: 1` and `totalPages: 816`.
- Changed an unconfigured `/v2/agent` request from an opaque 500 to a clear
  503 configuration response.
- Extended only `firecrawl_request.py` with health metrics, bounded direct
  crawl polling, saved output, envelope unwrapping, source-free metrics, and
  explicit User-Agent support. It now rejects non-positive PDF page caps,
  exits nonzero for failed or cancelled crawls, and writes a terminal,
  timeout, or HTTP-error artifact when an output target is requested.
- Removed benign Compose unset-variable warnings by giving optional
  integrations empty defaults. Queue capacity and CRE resource-profile
  settings were not changed.
- Updated healthcheck evidence generation, MCP JSONL framing, stale-smoke
  selection, capability documentation, and the two agent skills. The
  repository skills were synced to `~/.agents/skills`; existing non-symlink
  Claude skill directories were deliberately preserved.

## AGENTIC-2253 disposition

| Area | Result |
| --- | --- |
| AGENTIC-2254, agent-safe crawl polling | Addressed for local agents with the existing helper; official CLI behavior was intentionally not forked. |
| AGENTIC-2255, response envelopes | Addressed at the helper output boundary with opt-in `--unwrap`; default remains the API envelope. |
| AGENTIC-2256, heading and DOCX quality | Still requires a safe representative fixture and quality acceptance criteria. No generic heading inference or parser replacement was made. |
| AGENTIC-2257, RSS/XML | Tool-selection documentation: use an HTTP/XML feed parser, not generic HTML scraping. |
| AGENTIC-2258, search backend order | Documented existing upstream order; no paid or external backend configuration was enabled. |
| AGENTIC-2259, map-first discovery | Documented as the preferred hub-discovery workflow. |
| AGENTIC-2260, helper ergonomics | Addressed by extending the existing helper only. |
| AGENTIC-2261, Compose capacity/warnings | Optional-variable warnings addressed; capacity remains the observed upstream fallback of two. |
| AGENTIC-2262, PDF page cap | Fixed and verified against the actual local API. |

## Verification

- API focused harness: `pnpm harness vitest run ...pdfParse.test.ts ...agent.test.ts` passed 2 tests; `pnpm run build` passed. The post-harness Compose stack and healthcheck remained green.
- Local runtime smoke matrix: 9 core passes, 0 failures, 4 expected optional skips for root, scrape, map, search, fast PDF parse, batch polling, crawl polling, queue status, and active crawls.
- Direct helper PDF cap: public 816-page PDF returned `numPages: 1`, `totalPages: 816`, and 76 Markdown characters under `--max-pages 1`.
- Operations suites: CLI wrapper 5, MCP wrapper 4, healthcheck 2, capability matrix 1, Compose optional-env 1, and direct-helper 30 tests passed. `docker compose config --quiet` passed.
- CRE boundary checks: resource-profile tests 3 passed, collector TypeScript typecheck passed, and collector unit tests passed 768 tests. No CRE collector, SQL, source adapter, scheduler, data, or resource-profile file was changed.

Raw local artifacts are ignored under `tasks/tmp/20260813-final-validation/`.
Detailed independent reports are retained beside this file: API validation,
CLI/MCP validation, parser/ops validation, Linear audit, upstream-first
research, CRE boundary review, and adversarial implementation review.

## Remaining gates

- `/v2/agent`, browser, AI extraction, and OCR remain configuration-gated
  capabilities. Their unavailable-state behavior is intentional and now
  diagnosable; enabling them requires their separately managed service/model
  configuration.
- The full CRE Python suite has one pre-existing non-hermetic failure caused
  by a missing ignored Cushman artifact (2084 passed, 1 failed). This batch
  does not create or substitute that artifact.
- AGENTIC-2256 needs an owner-approved non-sensitive DOCX/PDF quality fixture
  before changing parser behavior.

## Rollback

Revert the implementation commit, then rebuild only the API service with
`docker compose build api && docker compose up -d --force-recreate api`.
The change contains no migration, data write, scheduler, credential, or
capacity-profile mutation.
