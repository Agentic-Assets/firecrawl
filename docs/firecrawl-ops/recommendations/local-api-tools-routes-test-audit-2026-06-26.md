# Local API Tools, Routes, and Test Coverage Recommendations (2026-06-26)

## Executive Summary

The rebuilt local Firecrawl stack is in good shape for the core self-hosted workflows. The Docker healthcheck passed, the API root responded on `http://localhost:3002`, and representative v2 routes worked locally for scrape, map, search, parse, crawl, batch scrape, queue status, and active crawl visibility.

The main improvement opportunity is not the upstream API surface. It is the fork-local operations layer around that API: shell wrappers, Python helpers, swarm scripts, MCP wrapper behavior, and repeatable local smoke evidence. Those tools work in live checks, but several of them are not covered by direct unit tests or a single repeatable audit command.

## Verified Today

- Rebuilt API image: `firecrawl-api:latest` with image id `42058045f1ed`.
- Recreated the API container with `docker compose up -d --force-recreate api`.
- Ran `scripts/firecrawl-ops/firecrawl_healthcheck.sh`; it passed.
- Verified API root at `GET /`.
- Verified `POST /v2/scrape` with `https://example.com`.
- Verified `POST /v2/map` with `https://example.com`.
- Verified `POST /v2/search` through the local helper.
- Verified `POST /v2/parse` against `apps/test-site/public/example.pdf`.
- Verified `POST /v2/batch/scrape`, then polled `GET /v2/batch/scrape/:jobId` to completion.
- Verified `POST /v2/crawl`, then polled `GET /v2/crawl/:jobId` to completion.
- Verified `GET /v2/team/queue-status`.
- Verified `GET /v2/crawl/active`.
- Verified `scripts/firecrawl-ops/firecrawl_request.py` for scrape, map, search, and parse.
- Verified `scripts/firecrawl-ops/firecrawl_cli.sh` for scrape.

## Route Coverage Read

The v2 router registers the important local API surfaces:

- Core content routes: `POST /v2/scrape`, `GET /v2/scrape/:jobId`, `POST /v2/parse`, `POST /v2/map`, `POST /v2/search`.
- Async collection routes: `POST /v2/crawl`, `GET /v2/crawl/:jobId`, `DELETE /v2/crawl/:jobId`, crawl websocket status, crawl errors, active crawls, and ongoing crawls.
- Batch scrape routes: `POST /v2/batch/scrape`, `GET /v2/batch/scrape/:jobId`, `DELETE /v2/batch/scrape/:jobId`, and batch errors.
- Runtime visibility routes: `GET /v2/team/queue-status`, `GET /v2/concurrency-check`, credit usage, token usage, and activity.
- Optional or service-dependent routes: extract, agent, browser/interact, monitor, support proxy, research proxy, and x402 search.

Not every registered route should be expected to work in the local stack without more services or credentials. The core non-AI routes worked today. Browser/interact routes require `BROWSER_SERVICE_URL`. Agent routes require `EXTRACT_V3_BETA_URL`. AI-backed extract, summaries, schema extraction, and crawl parameter preview require a valid model profile. Research, support, x402, and monitor routes depend on extra configured backends or hosted features.

## Test Coverage Assessment

Coverage is ample for upstream API behavior and the CRE collector workstream, but not yet ample for the fork-local ops tools.

- `apps/api/src/__tests__` currently contains 75 TypeScript test/spec files.
- API tests include v2 snips for scrape, map, crawl, batch scrape, parse, parser paths, search, search feedback, scrape cache, scrape formats, scrape viewport, browser-like scrape, and agent auth discovery.
- Controller-level tests also exist for v2 agent status, browser billing, feedback persistence, and crawl behavior.
- `scripts/firecrawl-ops/cre_collector` has broad Python and TypeScript coverage for parsing, enrichment, gate decisions, data quality guards, source adapters, status transitions, history, and shell-script syntax.
- `scripts/firecrawl-ops/tests/test_local_firepdf_ocr_service.py` covers the local OCR adapter.

The gaps are concentrated in these fork-local files and workflows:

- `scripts/firecrawl-ops/firecrawl_request.py` has live smoke coverage and some indirect usage in CRE parser tests, but no focused unit test suite for argument parsing, request payload building, multipart parse behavior, save-field behavior, error handling, or output path behavior.
- `scripts/firecrawl-ops/firecrawl_cli.sh` works in live smoke checks, but has no automated guard for CLI package drift or caller working-directory preservation.
- `scripts/firecrawl-ops/firecrawl_mcp.sh` is documented, but needs an automated stdio handshake or list-tools smoke check.
- `scripts/firecrawl-ops/firecrawl_healthcheck.sh` is useful and passed today, but its result is not captured into a structured artifact by default.
- `scripts/firecrawl-ops/crawl_swarm.py` and `scripts/firecrawl-ops/firecrawl_swarm_pipeline.py` have important local workflow logic, but should get fixture-based unit tests that do not require live network calls.

## Recommended Improvements

1. Add a one-command local smoke matrix.

   Create `scripts/firecrawl-ops/local_api_smoke_matrix.py` using only the Python standard library. It should test scrape, map, search, parse, crawl submit/status, batch submit/status, queue status, active crawls, and expected optional-service failures. It should write both JSON and Markdown under `tasks/tmp/local-api-smoke/`.

2. Add direct unit tests for `firecrawl_request.py`.

   Suggested file: `scripts/firecrawl-ops/tests/test_firecrawl_request.py`. Mock HTTP calls and assert payloads for scrape, map, search, parse multipart upload, `post`, `--out`, `--out-dir`, `--save-fields`, `--print-paths`, HTTP errors, invalid JSON, and timeout handling.

3. Add shell-wrapper tests for `firecrawl_cli.sh`.

   Suggested file: `scripts/firecrawl-ops/tests/test_firecrawl_cli_wrapper.py`. Stub `npx`, verify `--api-url http://localhost:3002`, verify `FIRECRAWL_CLI_PACKAGE` override, and verify the wrapper preserves caller cwd for local file parse paths.

4. Add an MCP wrapper smoke test.

   Add a small script or pytest that starts `scripts/firecrawl-ops/firecrawl_mcp.sh`, sends an MCP initialize request over stdio, then confirms the tool list includes expected Firecrawl tools. Keep this as an opt-in local smoke if the package install path depends on network.

5. Add fixture-only swarm tests.

   Suggested tests:

   - `scripts/firecrawl-ops/tests/test_crawl_swarm.py` for URL ranking, map result normalization, deduplication, failure accounting, and output artifact names.
   - `scripts/firecrawl-ops/tests/test_firecrawl_swarm_pipeline.py` for repo discovery, retry accounting, model escalation decisions, and safe no-network dry-run behavior.

6. Generate a local capability matrix.

   Add a generated Markdown file or script output that compares `apps/api/src/routes/v2.ts`, `docs/firecrawl-ops/references/tools-capabilities.md`, and live probe results. Mark each route as `works locally`, `needs model env`, `needs optional service`, `hosted-only`, or `not tested`.

7. Make the healthcheck produce durable evidence.

   Extend `scripts/firecrawl-ops/firecrawl_healthcheck.sh` or wrap it so every serious local audit writes:

   - Docker compose service status
   - API root response
   - scrape smoke result
   - image id
   - timestamp
   - pass or fail summary

8. Add Docker and pnpm config guards.

   Add a lightweight CI-safe check that verifies pnpm config stays in `apps/api/pnpm-workspace.yaml`, Docker build still succeeds with container-side native dependencies, and host pnpm hooks do not require local FoundationDB headers.

9. Add parse canaries.

   Keep `apps/test-site/public/example.pdf` as a fast parse canary. Add one scanned or table-heavy fixture when licensing permits, then run parse in `fast`, `auto`, and optional local OCR mode through the benchmark script.

10. Update skills after tool changes.

   Any change to wrappers, route expectations, optional-service status, or model routing should be followed by `scripts/firecrawl-ops/sync_agent_skills.sh` and a diff check against `.agents/skills/firecrawl-ops` and `.agents/skills/firecrawl-local-api`.

## Priority

- P0: No blocking local API issue found in the core stack.
- P1: Add the smoke matrix and direct `firecrawl_request.py` tests.
- P1: Add fixture-based swarm tests for the recovered helper scripts.
- P2: Add CLI wrapper and MCP wrapper tests.
- P2: Generate the local capability matrix from routes, docs, and probes.
- P3: Enable browser, agent, support, research, x402, or monitor routes only if those workflows become active local requirements.

## Final Recommendation

The local API tools and the core local routes work for the self-hosted Firecrawl use cases this fork is built around. The test base is strong for upstream API behavior and strong for the CRE collector. The next best improvement is to promote today's manual smoke evidence into repeatable local automation, then add focused unit tests around the fork-specific wrappers and swarm scripts.
