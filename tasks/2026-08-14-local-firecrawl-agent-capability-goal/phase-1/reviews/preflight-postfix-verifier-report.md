# AGENTIC-2277 Post-remediation Verification

## Scope

Independent read-only verification on 2026-08-14 of the consolidated
`feat/local-firecrawl-agent-capabilities` worktree. I reviewed the goal, the
Phase 1 safety contract, both finder reports, the skeptic remediation report,
the current preflight source/schema/tests, and the documentation. I did not
start services, contact the API, edit source or configuration, resolve a
package, access Linear, or touch CRE paths.

## Result

The seven previously confirmed safety findings are remediated in the current
preflight. One narrow, forward-compatibility defect remains before this can be
called fully complete.

### Confirmed: static package evidence can become `ready` without a doctor

**Priority: P2.** `package_capability()` returns `ready` when a supplied exact
spec textually matches a wrapper default
(`scripts/firecrawl-ops/local_agent_preflight.py:405-415`). That admits
`--require cli` or `--require mcp` without a fresh compatibility-doctor
result, package resolution, or protocol check. The current consolidated
wrappers use the doctor dynamically, so this branch is not reachable in the
present wrapper source; it is nevertheless a latent contract violation if a
future direct exact-pin wrapper is introduced.

The Phase 1 safety review defines `ready` for `cli` as a fresh successful
doctor result for the exact manifest version, and explicitly rejects treating a
static manifest as a healthy toolchain. The documentation likewise calls the
package declarations evidence only. Change the matching-wrapper outcome to
`degraded` (for example, `immutable_package_spec_declared_not_doctor_verified`)
until a separately versioned, body-free doctor receipt is consumed. Add a
fixture with an exact wrapper default and assert both capability states remain
non-ready and `--require` exits 1.

### Deferred hardening: queue occupation should be explicit before a future ready state

`async_jobs` currently always fails closed because an untrusted smoke artifact
ends in `degraded` (`local_agent_preflight.py:391-403`). It correctly rejects a
nonzero active-crawl count. Before a trusted smoke producer can ever change
that final state to `ready`, require an observed idle queue as well: the
current `queue_ready` test only requires a numeric `jobs_in_queue` field
(`:376-378`), not a zero queue/active-job count. This is not a present
readiness bypass, but it is a required guard for the Phase 1 rule that an
active queue state blocks a bounded pilot.

## Refuted prior findings

- **Proxy escape:** refuted. The production opener is built with
  `ProxyHandler({})` plus `NoRedirectHandler` (`:57-67`), and the regression
  test asserts an ambient proxy cannot configure it.
- **Redirect escape:** refuted. Redirect requests return `None` and are
  projected as an HTTP failure; only the three literal loopback GET paths are
  constructed (`:50-67`, `:324-331`).
- **Misidentified API root:** refuted. The boolean projection requires the
  exact `"Firecrawl API"` message (`:297-300`); the misleading-root fixture
  fails closed.
- **Non-RFC3339 smoke time:** refuted. A strict UTC RFC3339 lexical pattern is
  checked before parsing (`:43-47`, `:90-98`), including the space-separated
  negative fixture.
- **Untrusted smoke / active crawl readiness:** refuted. Fresh caller-supplied
  smoke remains `degraded`; active crawls get the explicit
  `active_crawls_present` reason (`:391-403`, test lines 238-257).
- **Digest omission or secret output:** refuted. The digest includes canonical
  redacted environment state only (`:205-217`, `:443-447`), while tests inject
  secrets and response bodies and assert none reaches the document.
- **Mutation or package resolution:** refuted for the current source. The
  source has no process/package client imports, and the focused guard test
  rejects path mutations in both normal and offline paths.

## Verification performed

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest scripts/firecrawl-ops/tests/test_local_agent_preflight.py -v` — 13 tests passed.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider scripts/firecrawl-ops/tests/test_local_agent_preflight.py -q` — 13 tests passed, 17 subtests passed.
- `git diff --check` — passed.
- A static-only command with exact CLI/MCP candidate pins remained non-ready in
  the current dynamic-wrapper implementation; no host request or package
  resolution was performed.

No host or AI-backed claim is made by this verification.
