# AGENTIC-2279 Agent-Safe Pilot Implementation Report

## Scope

Implemented the bounded `--agent-safe` mode in the existing
`scripts/firecrawl-ops/firecrawl_request.py` helper only. No alternate client,
runtime configuration, Docker, OrbStack, `.env`, CRE collector, EQUIRE,
Supabase/Postgres, launchd, or Linear change was made.

## Delivered contract

- Validates before model-profile dispatch, HTTP, subprocess, or output writes.
- Accepts only canonical local HTTP origins on port 3002: `localhost`,
  `127.0.0.1`, and `::1`.
- Requires `--metrics-only` and a relative `--receipt-dir` beneath `tasks/`.
  It rejects full-output options, path printing, profile/Docker controls,
  arbitrary `post`, AI/structured/summary/query formats, OCR/asynchronous PDF
  controls, proxy/header controls, and unsafe targets.
- Restricts map to limit one and crawl to one page, concurrency one, one
  explicit allowlisted path, and approved non-AI formats. A crawl submission is
  sent once only; a successful response lacking a job id records a body-free
  `unknown` receipt and stops.
- Persists projected metrics plus a redacted receipt manifest. The manifest has
  schema/run identifiers, the local origin, helper and tooling-manifest SHA-256
  digests, a hashed input reference, bounds, elapsed time, disposition, zero
  retained body bytes, and a portable `artifact_ref` plus SHA-256. It omits
  response bodies, headers, tokens, raw target URLs, and absolute host paths.

## Verification

- `python3 -m py_compile scripts/firecrawl-ops/firecrawl_request.py scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py`
- `python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py scripts/firecrawl-ops/tests/test_firecrawl_request.py scripts/firecrawl-ops/tests/test_firecrawl_request_coverage.py` — 50 tests passed.
- `python3 -m pytest -q scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py scripts/firecrawl-ops/tests/test_firecrawl_request.py scripts/firecrawl-ops/tests/test_firecrawl_request_coverage.py` — 50 passed, 53 subtests passed.
- Focused `pytest-cov` run: nine new safety tests and 35 subtests passed. The
  dynamic helper load measured 60% across the whole legacy helper and 99% for
  the new focused test module; the remaining helper lines are existing broad
  operator modes outside this agent-safe change.
- `uvx ruff check scripts/firecrawl-ops/firecrawl_request.py scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py` — passed.
- `uvx ruff format --check scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py` — passed. The existing helper is not Ruff-formatted globally, so its formatter run was intentionally not applied to avoid a broad unrelated rewrite.
- `git diff --check` — passed.

## Deferred gates

No local host pilot was run. Per the goal, a live scrape/map/crawl/PDF pilot is
blocked pending fresh AGENTIC-2277 preflight evidence, a successful AGENTIC-2278
compatibility doctor, an idle queue, and a bounded health prerequisite.
