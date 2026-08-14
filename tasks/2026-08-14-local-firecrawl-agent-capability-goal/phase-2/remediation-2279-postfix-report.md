# AGENTIC-2279 agent-safe postfix remediation report

Date: 2026-08-14

## Scope

This pass addressed every confirmed finding in the independent postfix review
without exercising a live Firecrawl service, Docker, `.env`, CRE workflow, or
Linear. It keeps `--agent-safe` a deliberately tiny first-pilot surface and
does not alter normal helper metrics, operator crawl-status behavior, profiles,
or CRE collection.

## Remediation

- All safe HTTP boundaries now treat every non-2xx response, including 302, as
  `http_rejected`, write the one redacted terminal result after dispatch, and
  exit nonzero before a crawl ID can be read or polled. This covers health,
  scrape, map, parse, crawl submit, and crawl polling.
- Safe POST authorization no longer consumes caller prerequisite artifacts.
  The helper itself runs the checked-in GET-only preflight, validates a fresh
  ready/idle result, then runs the normal manifest-pinned compatibility doctor
  with `--run` against the exact safe loopback origin. The doctor executes its
  bounded map/MCP checks and never uses `@latest`; queue and active-crawl GETs
  are immediately rechecked before the POST.
- Both preflight and direct rechecks now require an explicit `success: true`,
  in addition to valid zero queue/active values. Missing, false, malformed, or
  nonzero values fail closed.
- Safe recipe guards use explicit presence checks, so false, zero, and empty
  rendering/cache/header controls cannot bypass the fixed contract.
- Receipt output rejects symlinks in every existing `tasks/` component before
  dispatch. Metrics and manifests are schema-validated and written through
  same-directory temporary files with `os.replace`; the manifest is written
  last as the terminal commit marker. A receipt write failure is finite and is
  not retried by the outer error handler.
- The minimal interface-ladder note now tells agents not to supply evidence
  artifacts and explains the bounded, helper-run validation flow.

## Focused regression coverage

`test_firecrawl_agent_safe.py` now mocks and proves:

- no proxy / no redirect transport for JSON and multipart;
- 302 rejection at every safe boundary with no follow-up poll;
- a hand-authored artifact cannot authorize a POST;
- normal, manifest-pinned doctor invocation uses the current safe origin;
- false/missing queue or active `success`, stale/unready producer evidence,
  false/zero/empty recipe options, and symlink paths cause no POST;
- invalid crawl IDs, cancellation, timeout, malformed status, and transport
  diagnostics produce only finite redacted outcomes;
- opaque closed receipts reject unknown fields/nonpilot bounds; a failed
  manifest write leaves no final receipt.

`test_local_agent_preflight.py` additionally verifies the producer rejects
queue/active responses without explicit `success: true`. The existing normal
helper tests remain in the focused run.

## Verification

- `python3 -m py_compile` on the helper, preflight, doctor, and five focused
  test modules — passed.
- `python3 -m unittest -v` on `test_firecrawl_request.py`,
  `test_firecrawl_request_coverage.py`, `test_firecrawl_agent_safe.py`,
  `test_local_agent_preflight.py`, and `test_firecrawl_compatibility_doctor.py`
  — **94 passed**. The doctor test uses an ephemeral loopback fixture only.
- `uvx ruff check` plus `ruff format --check` on the changed agent-safe helper,
  compatibility doctor, and focused agent-safe/doctor tests — passed.
- Combined `pytest-cov` coverage on the five focused modules — **94 passed**;
  measured source coverage: helper 92%, preflight 85%, doctor 75% (87% total).
  The helper's remaining lines are ordinary legacy operator paths outside this
  first-pilot boundary. Annotated output was written only to temporary storage.
- `git diff --check` — passed.

## Remaining gate

This is mocked local proof only. A live first-pilot POST remains separately
operator-gated: it must use the fixed fixture, fresh same-process prerequisites,
and an observed idle shared queue. No live run was performed here.
