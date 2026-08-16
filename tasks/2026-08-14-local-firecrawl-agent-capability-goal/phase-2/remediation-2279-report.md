# AGENTIC-2279 agent-safe remediation report

Date: 2026-08-14

## Scope and outcome

Remediated the confirmed P1/P2 findings for the deliberately tiny
`--agent-safe` pilot in `scripts/firecrawl-ops/firecrawl_request.py`. The
ordinary helper remains available for operator workflows; safe mode adds a
separate projection and dispatch boundary rather than narrowing normal
`response_metrics()` or `crawl-status` behavior.

The pilot now permits only these fixed recipe inputs:

- loopback API origins on HTTP port 3002;
- `https://example.com/` for scrape, map, and crawl;
- the tracked `apps/test-site/public/example.pdf` fixture at SHA-256
  `f6edcd8a1b4f7cb85486d0c6777f9174eadbc4d1d0d9e5aeba7132f30b34bc3e` for
  fast one-page parse.

It rejects standalone `crawl-status`, generic `post`, AI/OCR/raw-output and
profile controls, alternative formats and path allowlists, caller-controlled
receipt paths, mutable options, query/fragment targets, and all relaxed
timeout/poll/crawl bounds before profile, HTTP, subprocess, or output work.

## Safety controls

- Safe JSON and multipart requests use an opener with `ProxyHandler({})` and a
  redirect-rejecting handler. Normal requests retain their established
  transport behavior.
- Every safe POST requires fresh (at most 45 seconds old) fixed-path,
  body-free evidence: a preflight with `base_http=ready`, successful zero
  queue and active-crawl observations, plus a normal passed compatibility
  doctor result tied to the current tooling-manifest digest. The doctor now
  emits strict UTC `observed_at` at result completion.
- Immediately before the POST, safe mode repeats bounded safe GET checks for
  queue zero and empty active crawls. Malformed, nonzero, unknown, HTTP-failed,
  or transport-failed observations fail closed.
- Safe crawl polling uses only a same-process submitted ID after a strict
  identifier check. Missing or malformed IDs write one finite `unknown_submit`
  receipt and never issue a follow-up poll.
- Safe metrics use a per-recipe closed schema. They omit server IDs, arbitrary
  statuses, messages, bodies, URLs, paths, and headers. Receipts persist only
  opaque artifact identifiers, fixed input classification, bounds, and
  schema-validated prerequisite digest/timestamp values. Every allowed request
  has one terminal body-free receipt; input/prerequisite rejection writes none.

The minimal interface-ladder note is in
`docs/firecrawl-ops/references/agent-tooling-firecrawl.md`. No CRE collector,
EQUIRE process, Docker/OrbStack runtime, `.env`, model profile, Linear record,
or live Firecrawl endpoint was changed or exercised.

## Verification

- `python3 -m py_compile scripts/firecrawl-ops/firecrawl_request.py scripts/firecrawl-ops/firecrawl_compatibility_doctor.py scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py scripts/firecrawl-ops/tests/test_firecrawl_compatibility_doctor.py`
- `python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_request.py scripts/firecrawl-ops/tests/test_firecrawl_request_coverage.py scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py` — 54 passed.
- `python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_compatibility_doctor.py` — 17 passed, including an ephemeral-loopback no-proxy regression.
- `uvx ruff check` on the helper, doctor, and focused tests — passed.
- `uvx ruff format --check` on the new/modified tests and doctor — passed. The
  pre-existing helper has a repository-wide formatter diff, so it was not
  reformatted wholesale.
- `uvx --with pytest-cov pytest` on the four focused modules — 70 passed and
  77 subtests passed. The source-directory report measured
  `firecrawl_request.py` at 92%; the remaining uncovered lines are ordinary
  legacy operator paths outside this pilot.
- `git diff --check` — passed.

## Remaining gate

This is code and mocked-test proof only. A live safe pilot remains blocked
until fresh fixed-path preflight and normal compatibility-doctor evidence are
generated, the direct idle recheck is zero, and the parent process authorizes
the separate bounded host run.
