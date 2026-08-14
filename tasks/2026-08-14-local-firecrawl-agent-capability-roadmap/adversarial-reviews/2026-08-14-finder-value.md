# Adversarial Finder Report: Local Firecrawl Agent Roadmap Value and Simplicity

**Date:** 2026-08-14

**Review mode:** finder pass only. A separate skeptic must independently try to refute every candidate before any plan or Linear change.
**Scope:** user value, simplicity, sequence, measurable outcomes, and reuse of the checked-in upstream API/SDK/CLI/MCP surfaces. No runtime, environment, CRE, Linear, or plan mutation was performed.

## Evidence basis and method

- Reviewed the proposed [local-agent adoption plan](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md), its eleven mapped Linear packets, the capability/agent-tooling references, and the checked-in helper, CLI, MCP, model, API, and SDK sources.
- The docs worktree was at `69e1dbe4a` on `docs/local-firecrawl-agent-roadmap`. This finder did not claim current OrbStack health and did not inspect secrets or invoke a model, package installer, or API request.
- The evaluation rule was deliberately narrow: retain only changes that improve a local agent's ability to choose and use an existing surface safely. Do not recommend a parallel client, remote runtime, daemon, scheduler, database/telemetry service, or CRE work.

## Candidate findings for skeptic review

| ID | Severity | Packets | Summary |
| --- | --- | --- | --- |
| F-VAL-001 | P1 | AGENTIC-2278 | Recorded package pins do not pin the default CLI/MCP execution path. |
| F-VAL-002 | P1 | AGENTIC-2260 | P1 `wait-job` scope includes deprecated v2 extract status instead of the current structured-output surface. |
| F-VAL-003 | P1 | AGENTIC-2277 | A context-free `ready` verdict can overstate readiness for AI/OCR or optional-service work. |
| F-VAL-004 | P1 | AGENTIC-2280 | Agent-facing command surfaces can still rewrite `.env` and recreate the shared API, contrary to the handoff boundary. |
| F-VAL-005 | P2 | AGENTIC-2280 | Model-routing documentation diverges from the configured Vercel Gateway snapshot IDs. |
| F-VAL-006 | P2 | AGENTIC-2279 | The public map pilot can pass without demonstrating a usable map-first decision. |
| F-VAL-007 | P2 | AGENTIC-2279 | The developer recipe omits existing official SDK watcher and bounded-pagination capabilities. |
| F-VAL-008 | P2 | AGENTIC-2277, 2260, 2278, 2279, 2280 | The release measures have no per-packet denominator or decision threshold, so they cannot determine whether the P1 sequence helped agents. |

### F-VAL-001 — Default package resolution remains unpinned

- **Severity:** P1.
- **Exact evidence:** The plan says AGENTIC-2278 must record explicit package versions and must not silently promote untested `@latest` ([plan lines 280-292](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#3-local-agent-pin-and-diagnose-local-firecrawl-cli-and-mcp-packages--agentic-2278)). The actual normal-path defaults are `firecrawl-cli@latest` in [firecrawl_cli.sh line 6](../../../scripts/firecrawl-ops/firecrawl_cli.sh) and `firecrawl-mcp@latest` in [firecrawl_mcp.sh line 6](../../../scripts/firecrawl-ops/firecrawl_mcp.sh). The capability reference also describes the CLI as `@latest` while offering an override only after breakage ([tools-capabilities lines 53-60](../../../docs/firecrawl-ops/references/tools-capabilities.md#cli-wrapper)).
- **Failure scenario:** The doctor proves `firecrawl-cli@X` and `firecrawl-mcp@Y`, but an agent later runs a wrapper with no environment overrides. `npx` resolves a newer package. A subtle CLI flag or MCP protocol change makes the verified recipe fail or change behavior, even though the team believes it is using the tested contract.
- **Recommended correction:** Make the verified non-secret package specs the checked-in defaults of the two existing wrappers. Keep `FIRECRAWL_CLI_PACKAGE` and `FIRECRAWL_MCP_PACKAGE` only as explicit upgrade-canary overrides. Extend AGENTIC-2278's acceptance proof to run each wrapper with those variables unset, report the resolved version on stderr, and distinguish package-resolution failure from local-API or protocol failure. This reuses the wrappers and doctor; it adds no new client or service.

### F-VAL-002 — `wait-job` spends P1 scope on a deprecated extract route

- **Severity:** P1.
- **Exact evidence:** Phase 2 and AGENTIC-2260 call for `wait-job` across crawl, batch scrape, and extract ([plan lines 147-160](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#phase-2-close-the-demonstrated-agent-ergonomics-gaps), [lines 265-278](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#2-local-agent-generalize-bounded-waiting-for-crawl-batch-scrape-and-extract--agentic-2260)). Upstream explicitly marks both `/v2/extract` and `/v2/extract/:jobId` deprecated and points users to `/v2/scrape` with a `json` format ([deprecations.ts lines 21-29](../../../apps/api/src/lib/deprecations.ts)). The capability guide likewise favors v2 scrape `json` for one-page structured extraction ([tools-capabilities lines 162-168](../../../docs/firecrawl-ops/references/tools-capabilities.md#practical-selection-guide)).
- **Failure scenario:** The first shared `wait-job` abstraction codifies an upstream-retiring contract, consumes test complexity across a third job shape, and encourages new agent workflows to depend on a route the API itself tells users to replace.
- **Recommended correction:** Make the first AGENTIC-2260 allowlist `crawl` and `batch-scrape` only. Direct one-page structured work to the official v2 scrape JSON surface; add extract support only if a named legacy consumer cannot migrate, and make that a separately justified compatibility subtask with a rejection/migration message. Preserve the current helper's bounded crawl behavior and use SDK lifecycle semantics as the terminal-state oracle.

### F-VAL-003 — Preflight readiness lacks a requested-capability contract

- **Severity:** P1.
- **Exact evidence:** AGENTIC-2277 requires fixtures for `ready`, `degraded`, `unavailable`, and `stale-evidence` ([plan lines 251-263](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#1-local-agent-add-read-only-preflight-capability-contract--agentic-2277)), while Phase 0 combines static route classes, GET-only API/queue checks, optional-service status, model-capability status, and prior-smoke freshness ([plan lines 103-121](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#phase-0-establish-a-truthful-local-operating-contract)). The capability reference shows that a reachable root/queue does not establish AI JSON, crawl params-preview, OCR, browser, or agent readiness ([tools-capabilities lines 35-39](../../../docs/firecrawl-ops/references/tools-capabilities.md#core-endpoints-self-hosted-api), [lines 148-154](../../../docs/firecrawl-ops/references/tools-capabilities.md#present-but-not-configured-locally)).
- **Failure scenario:** A preflight emits a top-level `ready` because root and queue GETs succeed. An agent then selects an AI JSON, OCR, or browser workflow whose provider or optional service was merely unknown or absent. The result is a preventable failed request and confusing diagnosis, not a truthful capability choice.
- **Recommended correction:** Define preflight output per named capability, for example `base_http`, `async_jobs`, `cli`, `mcp`, `ai_formats`, and `pdf_ocr`, each with `state`, `evidence_kind` (`static`, `GET`, prior smoke, or unknown), and `checked_at`. Let an optional read-only `--require <capability>` make the aggregate verdict fail closed when a requested capability is `unknown`, `stale`, or unavailable. Do not infer a model call or OCR success from the GET-only preflight. This is a JSON contract refinement, not a new transport layer.

### F-VAL-004 — The profile handoff is not enforced at the agent command boundary

- **Severity:** P1.
- **Exact evidence:** The plan says agents must not autonomously change model or OCR profiles because those operations rewrite the root environment and recreate the API ([plan lines 42-57](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#hard-scope)), and AGENTIC-2280 says actions must never happen automatically during active work ([lines 307-318](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#5-local-agent-document-exclusive-model-profile-and-ocr-capacity-handoff--agentic-2280)). Yet `firecrawl_cli.sh` exposes `--firecrawl-model-profile` and defaults to `RECREATE_API=1`, then calls `set_model_profile.sh` and `docker compose ... --force-recreate api` ([firecrawl_cli.sh lines 6-9](../../../scripts/firecrawl-ops/firecrawl_cli.sh), [lines 64-109](../../../scripts/firecrawl-ops/firecrawl_cli.sh)). The helper exposes the same `--model-profile` option and launches the same mutation path ([firecrawl_request.py lines 143-160](../../../scripts/firecrawl-ops/firecrawl_request.py), [lines 405-419](../../../scripts/firecrawl-ops/firecrawl_request.py)).
- **Failure scenario:** An agent treats a summary or JSON failure as a configuration task, supplies the advertised profile option, and restarts the shared API while another bounded job is running. A documented queue check is advisory after the command has already made a profile mutation possible.
- **Recommended correction:** Narrow the agent-facing CLI and helper to run-only operations. Keep `set_model_profile.sh` as the explicit, separately documented operator procedure and require a fresh human-authorized operator window before it is invoked. Update AGENTIC-2280's proof to show that both agent surfaces reject profile-change flags and that the standalone operator procedure requires recorded queue/active-work evidence, health check, canary, and handoff. This removes an accidental affordance while reusing the existing profile script.

### F-VAL-005 — The documented Gateway model IDs are stale relative to the configured profiles

- **Severity:** P2.
- **Exact evidence:** The model-routing reference names generic Gateway `deepseek/deepseek-v4-flash` ([model-routing lines 38-42](../../../docs/firecrawl-ops/references/model-routing.md#default-routing-policy)) and generic escalation IDs ([lines 26-36](../../../docs/firecrawl-ops/references/model-routing.md#default-routing-policy)). The configured Gateway profiles write `deepseek/deepseek-v4-flash-0731` and `deepseek/deepseek-v4-pro-0813` ([set_model_profile.sh lines 130-142](../../../scripts/firecrawl-ops/set_model_profile.sh)). The roadmap requires recording the explicit profile before an AI/OCR canary but does not identify a canonical profile-to-model record ([plan lines 162-177](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#phase-3-apply-model-and-ocr-work-only-in-an-exclusive-operator-window)).
- **Failure scenario:** An agent or operator follows the reference to reproduce a Gateway canary, records the generic model name, and later cannot reliably compare outcome, cost, or structured-output behavior against the actual snapshot selected by the profile setter.
- **Recommended correction:** In AGENTIC-2280, make the profile setter's exact non-secret provider/model mapping canonical and bring the model-routing reference into literal agreement: Gateway default `deepseek/deepseek-v4-flash-0731`, Gateway intelligent profile `deepseek/deepseek-v4-pro-0813`. Add a focused static regression test that checks the documented mapping against the setter. Do not inspect or log keys, switch profiles during the test, or add automatic escalation.

### F-VAL-006 — The map pilot does not prove a map-first decision is usable

- **Severity:** P2.
- **Exact evidence:** Phase 1 calls for a public scrape-and-map pilot with one known public URL ([plan lines 123-139](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#phase-1-adopt-three-bounded-pilots-before-adding-infrastructure)); AGENTIC-2279 accepts any fixture or host-local evidence ([plan lines 294-305](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#4-local-agent-publish-the-interface-ladder-and-bounded-agent-pilots--agentic-2279)). The existing smoke matrix defaults its map input to `https://example.com` and only verifies that `links`, if present, is a list ([local_api_smoke_matrix.py lines 266-275](../../../scripts/firecrawl-ops/local_api_smoke_matrix.py), [lines 505-516](../../../scripts/firecrawl-ops/local_api_smoke_matrix.py)). A zero-link response therefore passes the current smoke shape.
- **Failure scenario:** The pilot is recorded as successful although it produced no selectable candidates. Agents receive a "map first" recipe with no evidence that it can produce the constrained candidate list that the next scrape step needs.
- **Recommended correction:** Give AGENTIC-2279 two explicitly different proofs: a bounded public egress smoke, where zero links is an honest allowed outcome, and a deterministic synthetic/API-harness map fixture with a minimum expected candidate set and an asserted cap. Reuse `apps/test-site` assets and the existing test/harness infrastructure where available; do not start a persistent test service or add a new runtime. The pilot manifest should record which proof ran and whether it established reachability, map semantics, or both.

### F-VAL-007 — The official SDK recipe misses built-in progress and result-bounding tools

- **Severity:** P2.
- **Exact evidence:** The developer recipe says only to use an official SDK with submit/status/cancel lifecycle ([plan lines 222-226](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#developer-automation)). The checked-in JS SDK already exposes `startCrawl`, bounded `getCrawlStatus` pagination, result caps, and wait caps ([example_pagination.ts lines 17-33](../../../apps/js-sdk/example_pagination.ts)); it also exposes a timeout-bounded watcher ([example_watcher.ts lines 15-31](../../../apps/js-sdk/example_watcher.ts)). The Python v2 SDK likewise provides a watcher for `crawl` and `batch` with a timeout ([watcher.py lines 1-39](../../../apps/python-sdk/firecrawl/v2/watcher.py)).
- **Failure scenario:** An application agent uses the shell helper to obtain progress or manually reimplements status pagination, although the official SDK already supplies a typed, maintained lifecycle. That blurs the carefully intended "helper for shell agents, SDK for application code" boundary.
- **Recommended correction:** Add one concise application-only recipe to AGENTIC-2279: use `startCrawl` or `startBatchScrape`, bounded `get*Status` pagination for final retrieval, and an SDK watcher only when document-level progress is actually needed. Require explicit timeout/result caps and a cancellation policy. Retain `firecrawl_request.py` only for shell-stable artifacts and its documented API-only gaps.

### F-VAL-008 — Measures do not yet decide whether the P1 sequence succeeds

- **Severity:** P2.
- **Exact evidence:** The plan's release section lists useful signals such as preflight accuracy, saved job IDs, duplicate submissions, p95, receipt completeness, and package version ([plan lines 403-408](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#measures-and-release-gates)), but assigns no denominator, acceptance target, evidence artifact, or packet-level decision. The P1 packets otherwise have distinct fixtures and healthy-host expectations ([plan lines 251-318](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#linear-issue-packets)).
- **Failure scenario:** Work can be declared complete after a passing unit test or a single pilot even if agents still choose an unavailable capability, submit duplicate work, or run an unpinned package. There is no objective signal for whether to proceed from preflight to additional infrastructure.
- **Recommended correction:** Add a small checked-in P1 scorecard to the plan and the five P1 issue definitions, not a telemetry service. For each packet, state: the fixture/public-canary denominator, exact pass rule, durable body-free artifact, and next decision. Examples: all named preflight fixtures must emit the expected per-capability state; all mock terminal states must produce nonzero on non-success without bodies; both default wrapper pins must pass doctor; each of three pilots needs an explicit accept/manual-review/reject disposition; no profile action occurs from an agent surface. Use the scorecard to decide whether receipts or later conditional work is justified.

## Explicit no-findings

These packet-level decisions were reviewed and should remain as written unless the skeptic finds contrary live evidence:

| Packets | No finding retained | Evidence |
| --- | --- | --- |
| AGENTIC-2281 | The opt-in, redacted receipt boundary is proportionate. It already bans body/header/key/path/query retention and requires a retention gate before shared persistence. | [Plan lines 320-331](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#6-local-agent-add-opt-in-redacted-run-receipts--agentic-2281) |
| AGENTIC-2282 | The provenance sidecar is correctly deferred until a merged fallback and a named consumer exist; it does not overclaim field-level citation. | [Plan lines 333-346](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#7-local-agent-add-a-narrow-structured-output-provenance-sidecar--agentic-2282) |
| AGENTIC-188 | The named-consumer and no-OCR-on-read gates avoid a duplicate recommender and preserve the existing benchmark as the source. | [Plan lines 348-361](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#8-local-agent-standardize-consumption-of-pdf-benchmark-recommendations--agentic-188) |
| AGENTIC-195 | The params-preview packet correctly requires a separate explicit crawl and preserves caps. It is a P3 convenience, not a scheduler. | [Plan lines 363-374](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#9-local-agent-materialize-bounded-crawl-plans-without-auto-executing-them--agentic-195) |
| AGENTIC-2283 and AGENTIC-2284 | Both are founder-gated decisions with no build or network change authorized. Keeping sandbox and ingress work out of the P1 sequence is the simpler design. | [Plan lines 376-401](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#10-security-decide-whether-a-restricted-local-agent-sandbox-is-justified--agentic-2283) |
| Whole roadmap | The interface ladder, no-parallel-client rule, no-remote-runtime rule, no-scheduler/database rule, and CRE exclusion are clear and should remain non-negotiable. | [Plan lines 8-18](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#objective), [lines 42-57](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#hard-scope) |

## Suggested order after skeptic confirmation

1. Resolve F-VAL-001, F-VAL-002, and F-VAL-003 before starting implementation, because they determine the actual P1 interfaces and test contracts.
2. Resolve F-VAL-004 and F-VAL-005 in the model/OCR handoff packet before any AI-backed canary.
3. Fold F-VAL-006 through F-VAL-008 into the AGENTIC-2279 recipe/scorecard update; they add high-value proof without adding infrastructure.
4. Do not start the receipt, provenance, PDF-reader, crawl-plan, sandbox, or ingress work merely because this finder report exists.

## Verification and limits

- Verified all cited plan/source anchors in the docs worktree; no source file was edited.
- No local runtime, Docker, network, package resolution, model call, environment file, or Linear state was touched.
- **Blocker:** This is a finder pass by design. The eight candidates are not approved fixes until an independent skeptic assigns `CONFIRMED` or `REFUTED` against the cited sources.
- **Next decision:** Have a skeptic refute each candidate, then update only the confirmed plan and Linear packets in one coherent docs/tracking change.
