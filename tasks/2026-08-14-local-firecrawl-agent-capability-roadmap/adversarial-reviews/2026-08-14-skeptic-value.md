# Skeptic verdicts: local agent roadmap value and simplicity

**Reviewed:** 2026-08-14
**Scope:** V1 through V8 in `2026-08-14-finder-value.md`, tested against the
actual adoption roadmap and cited checked-in sources. This is separate from
`2026-08-14-skeptic-agentops.md`. No plan, Linear, runtime, environment, or
CRE state was changed.

## V1 — CONFIRMED

AGENTIC-2278 says to record explicit package versions and not silently promote
untested `@latest`, but its definition of done does not alter the ordinary
wrapper default. The CLI and MCP wrappers still default to
`firecrawl-cli@latest` and `firecrawl-mcp@latest`; their own tests assert those
defaults when the package variables are absent. A doctor run using an override
therefore does not pin the later agent execution path.

**Minimal safe correction:** Require a recorded exact package spec for the
agent path, either as the tested default or as a fail-closed requirement when
the setting is absent. Keep `@latest` only for a labelled human upgrade probe,
and test the unset-variable normal path separately from the upgrade path.

## V2 — CONFIRMED

The roadmap gives P1 `wait-job` acceptance tests to crawl, batch scrape, and
extract, while the API's primary deprecation registry explicitly marks both
`/v2/extract` and `/v2/extract/:jobId` deprecated in favor of v2 scrape JSON.
The local capability guide still describes extract as an asynchronous
multi-URL surface, but that residual capability does not identify a legacy
consumer that warrants expanding the first agent helper abstraction to a
retiring route. The planned wait operation is for known IDs, and no Phase 1
pilot or developer recipe depends on extract, so narrowing the initial
allowlist does not remove a roadmap pilot.

**Minimal safe correction:** Limit the initial AGENTIC-2260 allowlist to crawl
and batch scrape. Direct new one-page structured work to v2 scrape JSON, and
add extract waiting only under a separately justified legacy-compatibility
subtask.

## V3 — CONFIRMED

Phase 0 combines API and queue GET observations with static route classes,
optional-service state, model-capability status, and old-smoke freshness, yet
AGENTIC-2277 requires only one top-level ready/degraded/unavailable/stale
fixture set. The capability reference distinguishes base endpoint reachability
from configured browser, agent, and AI surfaces, and the plan correctly says a
GET-only preflight cannot make a model or OCR call. A top-level `ready` result
without a requested-capability contract can consequently overstate what an
agent may select.

**Minimal safe correction:** Define named capability states with their evidence
kind and checked time, and let a read-only `--require <capability>` fail closed
for unknown, stale, or unavailable requested functionality. Preserve the
GET-only boundary and do not infer AI or OCR success from basic health.

## V4 — CONFIRMED

The roadmap prohibits autonomous profile changes, but both agent-facing
execution wrappers still expose them. The CLI's advertised profile option runs
the profile setter and defaults to recreating the API; the helper exposes the
same option and executes the same mutation path. AGENTIC-2280 requires an
operator procedure and active-work checks, not rejection of those flags at the
agent command boundary.

**Minimal safe correction:** Make the agent-facing CLI and helper reject
profile-change and container-mutation options before any write or Docker call.
Keep the existing profile setter as a separately authorized operator procedure
with queue, health-check, canary, and handoff proof.

## V5 — CONFIRMED

The routing reference names the generic Gateway model
`deepseek/deepseek-v4-flash`, whereas the actual `gateway` profile writes the
snapshot `deepseek/deepseek-v4-flash-0731` and its structured-output fallback
`deepseek/deepseek-v4-pro-0813`; `gateway-pro` writes the latter snapshot as
its primary model. The reference also instructs operators to use exact
provider model IDs. Recording only a profile in a canary record does not
resolve this literal documentation-to-configuration mismatch for later
comparison of behavior or cost.

**Minimal safe correction:** Make the profile setter's non-secret
profile-to-provider/model mapping canonical, update the routing reference to
match the Gateway snapshots, and add a static mapping regression check. Do not
switch a profile or inspect keys during that check.

## V6 — REFUTED

The cited `local_api_smoke_matrix.py` is a transport-health smoke probe, not
the roadmap's map-first pilot: it deliberately accepts an empty links list so
that a reachable map route is not reported as broken merely because a mutable
public site has no candidates. The roadmap separately requires a known public
hub for the map-first recipe and tells the agent to select canonical URLs from
the returned map before scraping. A fixed nonzero-link assertion against the
public web would make the pilot less truthful and the proposed synthetic
semantic fixture is not required to prove the bounded agent recipe.

**No correction:** Keep the smoke probe's honest zero-link behavior. The
published map-first recipe should report an empty candidate set as an outcome,
not treat it as a successful source-selection result.

## V7 — REFUTED

The developer recipe already requires the official SDK, explicit timeout and
retry policy, and a submit/status/cancel lifecycle; that is the relevant
language-neutral boundary for an adoption roadmap. SDK watchers are optional
progress mechanisms for crawl and batch jobs, while bounded pagination is an
application retrieval choice, not a prerequisite for every application agent.
No named consumer or failure mode establishes that adding JS- and Python-
specific watcher guidance now improves the minimal interface ladder.

**No correction:** Retain the concise SDK rule. Add an application-specific
watcher or pagination recipe only when a named consumer needs document-level
progress or bounded result retrieval.

## V8 — CONFIRMED

The release section lists valuable metrics but supplies no denominator, pass
target, evidence artifact, or packet-level go/no-go rule. The separate packet
definitions provide individual fixtures, but they do not define how preflight
accuracy, duplicate-work rate, p95 duration, or package compatibility decide
whether the P1 sequence helped agents. As written, these measures cannot
support a reproducible decision to proceed, pause, or add later infrastructure.

**Minimal safe correction:** Add a small checked-in P1 scorecard stating, for
each packet, its fixture or public-canary denominator, exact pass rule,
body-free evidence artifact, and next decision. Reuse existing fixture and
host-proof outputs rather than adding telemetry.

## Summary

V1, V2, V3, V4, V5, and V8 are **CONFIRMED**. V6 and V7 are **REFUTED**: the
finder conflates a route-health smoke with a semantic map pilot, and it asks a
minimal language-neutral SDK contract to prescribe optional implementation
details without a named consumer.

## Verification

- Read the current roadmap, issue packets, recipes, and release-measures
  section.
- Read the capability guide; API deprecation registry and extract-status
  controller; CLI and MCP wrappers; helper/profile setter; model-routing
  reference; smoke matrix; and JS/Python SDK examples and implementations.
- The roadmap workspace sources match the operational sources inspected for
  these findings; its Compose difference is an unrelated structured-output
  fallback environment entry and does not affect this review.
- Made no runtime, environment, plan, Linear, or CRE mutation.

## Next decision

Before implementation, incorporate only V1 through V5 and V8 into their named
packets as a compact roadmap revision. Keep V6 and V7 out of the fix set;
revisit either only when a named consumer demonstrates the missing need.
