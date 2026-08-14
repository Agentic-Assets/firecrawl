# Skeptic verdicts: agent operations, security, and cost

**Reviewed:** 2026-08-14
**Scope:** AO-01 through AO-06 in `2026-08-14-finder-agentops.md`, tested
against the current Local Firecrawl Agent Adoption Plan and the cited local
source. No plan, Linear, runtime, or CRE state was changed.

## AO-01 — CONFIRMED

The roadmap limits the work to local agents, but its interface-ladder packet
does not require a local-origin check. The CLI and MCP wrappers accept ambient
`FIRECRAWL_API_URL` or `API_URL` before their localhost fallback, and the MCP
wrapper exports the inherited API key to that selected origin; the helper also
accepts `FIRECRAWL_API_URL` without a loopback validation. The plan's separate
ingress caveat does not constrain this outbound endpoint selection.

**Minimal safe correction:** Make AGENTIC-2279 require an agent-safe wrapper
mode that permits only named loopback origins before `npx` or HTTP execution,
with remote-origin rejection tests. Retain any non-local endpoint override as a
separate human-operated path, not an ambient agent setting.

## AO-02 — CONFIRMED

The hard scope prohibits autonomous model or OCR profile changes, but the two
agent-facing execution surfaces expose a profile mutation path today. The CLI
handles `--firecrawl-model-profile` by running `set_model_profile.sh` and, by
default, `docker compose ... --force-recreate api`; the helper exposes
`--model-profile` and performs the same calls. AGENTIC-2280 requires a
procedure and checks against automatic action during active work, but neither
that packet nor AGENTIC-2279 makes these agent-surface flags unavailable.

**Minimal safe correction:** In the agent-safe mode, reject profile, OCR, and
container-mutation options before invoking any mutation; test that rejection
makes no `.env` write or Docker call. Keep profile changes in a distinct,
explicitly authorized operator workflow.

## AO-03 — CONFIRMED

AGENTIC-2278 requires named package-version verification and says not to
silently promote an untested `@latest`, but neither its definition of done nor
AGENTIC-2279 closes the normal wrapper fallback. The current CLI defaults to
`firecrawl-cli@latest`, the MCP wrapper defaults to `firecrawl-mcp@latest`, and
their own wrapper tests assert those defaults when no package variable is set.
Recording an override for a doctor run therefore does not prevent a later
ordinary agent run from resolving a new package.

**Minimal safe correction:** Require the agent path to use a recorded exact
package version, either as a tested wrapper default or by failing closed when
the exact package setting is absent. Reserve `@latest` for a labelled,
human-run upgrade probe and test both paths.

## AO-04 — CONFIRMED

The Phase 1 crawl pilot calls for one "small" crawl and a deadline, but it does
not give a numeric page, concurrency, path, or output bound. The helper makes
`--limit` and `--max-concurrency` optional and forwards them only when given,
while the Compose defaults permit ten concurrent crawl requests and five jobs.
A polling deadline bounds the client wait, not the amount of work accepted by a
submitted crawl.

**Minimal safe correction:** Add an AGENTIC-2279 pilot fixture and recipe with
a positive hard page cap, explicit path or domain scope, an output bound, and a
documented low concurrency cap; for the first proof, use `limit: 1` and
`maxConcurrency: 1`. Have agent-safe crawl submission reject missing or
over-threshold caps before POSTing.

## AO-05 — CONFIRMED

The helper posts `/v2/crawl` through `request_json`, which has no caller-header
parameter, and its crawl parser provides no idempotency-key option. The local
v2 crawl route does install `idempotencyMiddleware`, which recognizes a UUID in
`x-idempotency-key` and rejects reuse, so the plan's duplicate-work rule lacks
an available server-side guard on its prescribed helper submit path. The SDK's
low-level HTTP helper can construct that header, although the inspected public
v2 option is on batch scrape rather than a documented crawl option.

**Minimal safe correction:** Add a UUID-validated helper crawl
`--idempotency-key` and pass it as `x-idempotency-key`; require a caller with
an uncertain result to retain and reuse that same opaque key until the request
is reconciled. Do not claim that storing only a hash is sufficient to retry the
request, and add first-submit and reused-key fixtures for the local route.

## AO-06 — REFUTED

The claimed conflict is not present in the roadmap: the hard scope excludes
bodies, raw HTML, headers, cookies, credentials, URL query strings, deal data,
and client documents, but does not exclude raw local paths. Phase 1 explicitly
calls its manifest a task-local execution artifact "until helper receipts are
implemented," whereas AGENTIC-2281 separately requires future receipts to
exclude raw local paths and gates retention and cleanup before shared
persistence. A task-local lifecycle policy may be a worthwhile separate
privacy decision, but the cited text does not make its absence a conflict in
this plan.

## Summary

AO-01 through AO-05 are **CONFIRMED**. AO-06 is **REFUTED** because it merges
two deliberately separate contracts and attributes a raw-path prohibition to
the hard-scope rule that is not there.

## Verification

- Read the actual roadmap and forward queue, including hard scope, Phase 1,
  AGENTIC-2278 through AGENTIC-2281, and release measures.
- Read the cited CLI, MCP, and helper execution paths and their wrapper tests.
- Read the local v2 crawl route, idempotency middleware and implementation,
  plus the JS SDK's header helper and exposed batch option.
- Per scope, made no runtime, plan, Linear, or CRE change.

## Next decision

Decide whether to amend the roadmap/issue definitions for the five confirmed
gaps before any AGENTIC-2279 pilot begins. Keep AO-06 out of the fix set unless
the owner separately chooses to impose retention and path-redaction rules on
task-local artifacts.
