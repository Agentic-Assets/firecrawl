# Finder report: agent operations, security, and cost

**Review role:** adversarial finder, not final verdict
**Reviewed:** 2026-08-14
**Scope:** the Local Firecrawl Agent Adoption Plan and forward queue, checked
against the currently checked-in local wrappers, helper, Compose file, SDK, and
API routes. No containers, network calls, CRE paths, or Linear records were
modified.

## Method

This pass follows the finder stage of `adversarial-pr-review`: each item below
is a discrete, falsifiable candidate finding for an independent skeptic to
confirm or refute. A source reference is evidence, not a claim that the local
runtime currently has the cited state.

## Candidate findings

### AO-01 — P1: The local-only API boundary is a convention, not an enforced agent contract

- **Severity:** P1
- **Exact evidence:** The plan limits the work to local agents on this Mac and
  excludes remote-agent access ([plan lines 42-45](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#hard-scope)). It nevertheless makes the CLI, MCP wrapper, and helper the agent
  interfaces ([plan lines 64-74](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#interface-selection-rules)). The CLI accepts `FIRECRAWL_API_URL` or `API_URL` before its localhost
  fallback ([firecrawl_cli.sh lines 4-6](../../../scripts/firecrawl-ops/firecrawl_cli.sh)), the MCP wrapper does the same and exports the API key to the selected
  endpoint ([firecrawl_mcp.sh lines 4-9](../../../scripts/firecrawl-ops/firecrawl_mcp.sh)), and the helper takes `FIRECRAWL_API_URL` as its `--api-url` default
  ([firecrawl_request.py lines 405-408](../../../scripts/firecrawl-ops/firecrawl_request.py)).
- **Failure scenario:** A prompt, inherited shell environment, or copied command
  sets `FIRECRAWL_API_URL` to a non-local endpoint. A local agent then sends
  source URLs, requests, or any inherited API credential to that endpoint while
  appearing to follow the published local-interface ladder.
- **Recommended correction:** Define an agent-safe local mode shared by all
  three wrappers. It must parse and allow only the approved loopback API origin
  by default, reject all other origins before any subprocess or HTTP request,
  and have negative tests for a remote URL. Preserve a separately documented
  operator-only override that requires an explicit human invocation rather than
  an ambient environment variable. Add the required mode to AGENTIC-2279's
  definition of done.

### AO-02 — P1: The prohibition on autonomous model or OCR changes is not enforced by the proposed agent surfaces

- **Severity:** P1
- **Exact evidence:** The plan says agents do not autonomously change model or
  OCR profiles because those operations rewrite the root environment and
  recreate the API ([plan lines 51-53](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#hard-scope)); it then directs agents to the CLI and helper
  ([plan lines 66-73](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#interface-selection-rules)). Both surfaces expose profile-changing options:
  `firecrawl_cli.sh` parses `--firecrawl-model-profile` and runs
  `set_model_profile.sh` followed by `docker compose ... --force-recreate api`
  ([firecrawl_cli.sh lines 64-107](../../../scripts/firecrawl-ops/firecrawl_cli.sh)); `firecrawl_request.py` accepts `--model-profile` and performs the same
  mutation ([firecrawl_request.py lines 143-161](../../../scripts/firecrawl-ops/firecrawl_request.py), [lines 405-418](../../../scripts/firecrawl-ops/firecrawl_request.py)).
- **Failure scenario:** An agent tasked with making an AI-backed request uses a
  profile option suggested by wrapper help. It changes `.env` and restarts the
  shared API while another task is queued, bypassing the proposed exclusive
  operator window and provider-cost approval.
- **Recommended correction:** Separate operator profile control from
  agent-facing execution. In agent-safe mode, reject all model/OCR/profile and
  container-mutation flags before parsing the target command; require a
  distinct, human-run operator command with an explicit authorization record
  for any change. Add negative tests proving no `.env` write and no Docker
  invocation on rejected agent commands, then make that enforcement a
  prerequisite for AGENTIC-2279 pilots rather than a Phase 3 convention.

### AO-03 — P1: The package doctor does not by itself remove the current `@latest` execution path

- **Severity:** P1
- **Exact evidence:** The forward queue calls for replacing unrecorded
  `@latest` dependence ([forward queue lines 15-17](../2026-08-14-local-agent-adoption-roadmap-forward-queue.md#hardening)). The plan's AGENTIC-2278 guard says to record explicit
  `FIRECRAWL_CLI_PACKAGE` and `FIRECRAWL_MCP_PACKAGE` versions
  ([plan lines 280-292](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#3-local-agent-pin-and-diagnose-local-firecrawl-cli-and-mcp-packages--agentic-2278)). However, when those variables are absent, the wrappers execute
  `firecrawl-cli@latest` and `firecrawl-mcp@latest`
  ([firecrawl_cli.sh line 6](../../../scripts/firecrawl-ops/firecrawl_cli.sh), [firecrawl_mcp.sh line 6](../../../scripts/firecrawl-ops/firecrawl_mcp.sh)).
- **Failure scenario:** A healthy-host doctor validates one version through an
  environment override, but an ordinary later agent invocation omits the
  variable. `npx -y` resolves a newer package, changing CLI commands or MCP
  tools without the compatibility test, potentially causing unexpected calls
  or protocol breakage.
- **Recommended correction:** Make AGENTIC-2278 choose and commit a tested,
  exact package version as the agent-wrapper default, or make agent-safe mode
  fail closed when the exact package variable is absent. Keep `@latest` only in
  an explicitly labeled human upgrade probe. Add a test that ordinary wrapper
  invocation resolves the recorded package and a separate test that an upgrade
  candidate cannot become the default without the doctor result.

### AO-04 — P1: The crawl pilot has no enforceable work or resource ceiling

- **Severity:** P1
- **Exact evidence:** The Phase 1 crawl pilot specifies only “one small public
  crawl,” a saved ID, deadline, metrics-only output, and failure behavior
  ([plan lines 132-139](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#phase-1-adopt-three-bounded-pilots-before-adding-infrastructure)). The helper accepts optional `--limit` and `--max-concurrency` with no
  agent-mode maximum ([firecrawl_request.py lines 781-797](../../../scripts/firecrawl-ops/firecrawl_request.py)). The current Compose defaults allow ten crawl
  concurrent requests and five concurrent jobs
  ([docker-compose.yaml lines 34-37](../../../docker-compose.yaml)).
- **Failure scenario:** An agent interprets “small” differently or follows an
  overly broad input. It submits a crawl with omitted or large limits and
  concurrency, consumes the shared queue and local browser capacity, and can
  starve the bounded PDF or AI canary work even though the poll deadline is
  finite.
- **Recommended correction:** Make the pilot fixture and agent recipe require
  a positive hard `limit`, path/domain scope, output-size ceiling, and
  `max-concurrency` at or below a documented pilot threshold. For the first
  host proof, use a static public fixture with `limit: 1` and
  `maxConcurrency: 1`. Have the helper's agent-safe crawl command reject an
  omitted or over-threshold cap before POSTing, with failure fixtures covering
  each bound. Record the accepted bounds in the body-free receipt/manifest.

### AO-05 — P1: The duplicate-submission rule lacks the available idempotency mechanism

- **Severity:** P1
- **Exact evidence:** The plan tells agents not to create duplicate work after
  an uncertain submission ([plan lines 76-78](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#interface-selection-rules)) and plans a helper crawl submit/poll pilot
  ([plan lines 134-136](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#phase-1-adopt-three-bounded-pilots-before-adding-infrastructure)). The helper submits `/v2/crawl` without an
  idempotency header ([firecrawl_request.py lines 164-178](../../../scripts/firecrawl-ops/firecrawl_request.py), [lines 702-713](../../../scripts/firecrawl-ops/firecrawl_request.py)). In contrast, the local v2 crawl
  route activates `idempotencyMiddleware` ([v2.ts lines 272-280](../../../apps/api/src/routes/v2.ts)), which checks `x-idempotency-key`
  ([shared.ts lines 273-291](../../../apps/api/src/routes/shared.ts)); the official v2 SDK exposes the corresponding
  header preparation ([httpClient.ts lines 169-172](../../../apps/js-sdk/firecrawl/src/v2/utils/httpClient.ts)).
- **Failure scenario:** The API accepts a crawl but the client times out before
  receiving the job ID. The agent cannot query a specific job, submits again,
  and runs two crawls. The plan's instruction to inspect “active work” cannot
  reliably correlate either crawl to the original intent.
- **Recommended correction:** Reuse the upstream mechanism: add a validated
  idempotency-key option to the helper's v2 crawl submit path, generate it once
  per caller intent, and persist only a hash or opaque correlation reference in
  the body-free manifest. Require retries after an uncertain result to reuse
  the same key; add local route tests for first submit, repeated submit, and
  timeout-before-response behavior. Prefer the official SDK's existing option
  for application code, as the plan already directs.

### AO-06 — P2: The temporary manifest and receipt proposal conflict with the plan's own redaction rule and omit a local lifecycle

- **Severity:** P2
- **Exact evidence:** The hard scope says logs and receipts exclude raw local
  paths, URL query strings, source bodies, headers, cookies, credentials, deal
  data, and client documents ([plan lines 54-55](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#hard-scope)). Before receipts exist, Phase 1 requires a
  task-local manifest containing an “artifact path/checksum”
  ([plan lines 141-145](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#phase-1-adopt-three-bounded-pilots-before-adding-infrastructure)). The P2 receipt definition tests only secret/body/header/key/raw-path/query/userinfo
  exclusion and defers retention and cleanup only before *shared* persistence
  ([plan lines 320-331](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#6-local-agent-add-opt-in-redacted-run-receipts--agentic-2281)).
- **Failure scenario:** A pilot manifest stores an absolute artifact path that
  identifies a user directory or client document location. The artifact itself
  may contain source content, but neither the manifest nor the output directory
  has a specified permission mode, TTL, cleanup owner, or test. The record can
  then be copied into a review artifact or survive on disk despite the stated
  body-free handoff goal.
- **Recommended correction:** Define one versioned, allowlisted manifest/receipt
  schema before the first pilot. Replace raw paths with a task-relative opaque
  artifact ID plus hash; state directory permissions, default TTL, cleanup
  owner, and the rule for keeping only hashes after expiry. Extend the redaction
  fixtures to cover absolute paths, home-directory expansion, symlinks, and
  error strings. Apply the lifecycle to task-local pilot artifacts as well as
  any future shared store, without introducing a telemetry service.

## No-findings record

No additional discrete finding was raised for automatic OCR retry, CRE scope,
or founder-gated ingress/sandbox mutation. The plan explicitly prohibits those
operations or leaves them decision-gated; the items above address the gaps
between those stated limits and the existing agent-facing execution surfaces.

## Suggested skeptic focus

For each candidate, verify the cited line and test whether the proposed
correction is already provided by an uninspected wrapper mode or task contract.
In particular, distinguish a human-only override from the agent-safe default
before accepting AO-01 through AO-03, and verify v2 idempotency behavior on a
local host before accepting AO-05.
