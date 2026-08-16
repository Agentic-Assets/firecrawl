# Adversarial Finder Report: Local Agent Roadmap Coherence

**Date:** 2026-08-14
**Review role:** finder only. These candidate findings require an independent
skeptic pass before any change is made.
**Reviewed:** `docs/firecrawl-ops/references/local-agent-adoption-plan.md`, its
forward queue, and the checked-in local operations scripts and routes named
below.
**Not reviewed or changed:** live runtime, Linear, CRE collector/scrapers,
database configuration, or protected CRE paths.

## Candidate findings

### F1 — P1: Unknown async submissions can still create duplicate batch or extract work

- **Plan evidence:** The interface rule applies to *any* asynchronous task and
  tells an agent with an uncertain submission to inspect status or active work
  before re-submitting ([plan lines 76-78](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L76-L78)).
  The proposed `wait-job` operation is explicitly for **known** IDs ([lines
  265-278](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L265-L278)).
- **Primary-source evidence:** The API exposes an active-work listing only for
  crawls (`GET /crawl/ongoing` and `GET /crawl/active`), while batch scrape and
  extract status routes both require `:jobId`
  ([`apps/api/src/routes/v2.ts` lines 289-374](../../../apps/api/src/routes/v2.ts#L289-L374)).
  Queue status returns aggregate counts, not job identities
  ([`queue-status.ts` lines 19-25 and 74-88](../../../apps/api/src/controllers/v2/queue-status.ts#L19-L25)).
  The only visible v2 submit route with `idempotencyMiddleware` is `/crawl`;
  `/batch/scrape` and `/extract` do not have it
  ([`v2.ts` lines 255-280 and 358-374](../../../apps/api/src/routes/v2.ts#L255-L280)).
- **Failure scenario:** A batch-scrape or extract POST reaches the server but
  the client loses the response before saving the job ID. The agent cannot
  inspect a status route without that ID, the aggregate queue count cannot
  identify its request, and a second POST creates duplicate work or a second
  model-backed extraction.
- **Recommended correction:** Replace the generic uncertain-submission rule
  with a fail-closed rule for every job family: persist the returned ID
  immediately; if no ID was received, record `outcome_unknown` with a
  body-free request fingerprint and do **not** automatically re-submit. Make
  `wait-job` accept known IDs only. Treat any idempotent-submit design as a
  separately verified API capability, rather than inferring it from crawl, and
  add transport-drop fixtures for crawl, batch scrape, and extract.

### F2 — P1: The package-doctor packet does not close the unpinned default used by normal agents

- **Plan evidence:** The plan makes exact CLI/MCP package evidence a P1 guard
  and says an untested `@latest` must not be silently promoted
  ([plan lines 280-292](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L280-L292)).
- **Primary-source evidence:** The normal wrappers still default to
  `firecrawl-cli@latest` and `firecrawl-mcp@latest`
  ([`firecrawl_cli.sh` line 6](../../../scripts/firecrawl-ops/firecrawl_cli.sh#L6),
  [`firecrawl_mcp.sh` line 6](../../../scripts/firecrawl-ops/firecrawl_mcp.sh#L6)).
  The local-agent skill likewise documents that a regular CLI call executes
  `firecrawl-cli@latest` unless an environment override is supplied
  ([`firecrawl-local-api` skill line 110](../../../.agents/skills/firecrawl-local-api/SKILL.md#L110)).
- **Failure scenario:** The doctor verifies one explicitly supplied version,
  but the next normal agent command omits the environment variable and silently
  resolves a newer package. A CLI or MCP protocol change can therefore bypass
  the verified contract and reintroduce the very incompatibility the packet is
  meant to detect.
- **Recommended correction:** Make AGENTIC-2278's definition of done require
  an enforceable normal-path policy: set both wrappers to a vetted exact
  `package@version` default (or load exact versions from one checked-in
  compatibility manifest), report those resolved values in diagnostics and
  receipts, and require an explicit, visibly labelled opt-in for `@latest`.
  Specify the package-upgrade/rollback procedure in the same packet.

### F3 — P2: Phase 0's package-version requirement conflicts with its static-and-GET-only promise

- **Plan evidence:** Phase 0 requires a **resolved** CLI and MCP package
  version when declared ([plan line 114](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L114)),
  yet also limits preflight to static reads and local API GET checks
  ([lines 119-121](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L119-L121)).
- **Primary-source evidence:** Both wrappers resolve packages through `npx -y`
  ([`firecrawl_cli.sh` line 109](../../../scripts/firecrawl-ops/firecrawl_cli.sh#L109),
  [`firecrawl_mcp.sh` line 11](../../../scripts/firecrawl-ops/firecrawl_mcp.sh#L11));
  their present defaults are `@latest`. Establishing what `@latest` resolves to
  can require registry access and npm-cache writes, neither of which is a
  static read or local API GET check.
- **Failure scenario:** A purportedly read-only preflight invokes package
  resolution to learn its version. On a cache miss it performs external network
  work and mutates the npm cache; on a disconnected host it fails for a reason
  unrelated to the local Firecrawl API.
- **Recommended correction:** Have Phase 0 report only the *declared package
  spec* from configuration and mark dynamic resolution as `unknown` when no
  immutable exact version is already recorded. Reserve installation/execution
  and protocol verification for the opt-in doctor in AGENTIC-2278.

### F4 — P2: The model-profile safety boundary is descriptive, not protected at the selected agent interface

- **Plan evidence:** The hard scope says agents do not autonomously change
  model or OCR profiles because the action rewrites the root environment and
  recreates the API ([plan lines 51-53](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L51-L53)).
  The plan nevertheless directs agents to the local helper for advanced
  options and polling ([lines 66-74](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L66-L74)).
- **Primary-source evidence:** Every helper command accepts `--model-profile`
  ([`firecrawl_request.py` lines 405-419](../../../scripts/firecrawl-ops/firecrawl_request.py#L405-L419))
  and invokes `set_model_profile.sh` followed by `docker compose ...
  --force-recreate api` when it is used
  ([lines 143-161](../../../scripts/firecrawl-ops/firecrawl_request.py#L143-L161)).
  The CLI wrapper exposes the equivalent profile option and recreation path
  ([`firecrawl_cli.sh` lines 15-28 and 96-106](../../../scripts/firecrawl-ops/firecrawl_cli.sh#L15-L28)).
- **Failure scenario:** An agent attempting an AI-backed pilot includes a
  documented profile flag. It mutates `.env` and recreates the shared API
  outside the exclusive operator window, interrupting unrelated in-flight
  work; the later queue-aware handoff packet cannot prevent this call.
- **Recommended correction:** Add an explicit enforcement decision to
  AGENTIC-2280 before AI-backed agent pilots: either remove profile-changing
  flags from agent-facing wrappers and retain `set_model_profile.sh` as the
  operator-only entrypoint, or require a narrowly validated, auditable
  operator-window gate before those flags execute. Until then, every agent
  recipe should state that profile flags are forbidden and must fail closed in
  its test fixtures.

### F5 — P2: The Phase 1 prerequisite lacks an agent-sized overall deadline

- **Plan evidence:** Phase 1 requires `firecrawl_healthcheck.sh` before the
  pilots ([plan lines 123-130](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L123-L130)),
  but the explicit timeout requirement is stated only for the subsequent
  public scrape/map pilot ([lines 132-139](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L132-L139)).
- **Primary-source evidence:** The healthcheck defaults to 12 API-root retries,
  each with `--max-time 90`, plus two-second sleeps
  ([`firecrawl_healthcheck.sh` lines 72-75 and 100-111](../../../scripts/firecrawl-ops/firecrawl_healthcheck.sh#L72-L75)).
  A failed root check can therefore consume roughly 18 minutes before the
  smoke request is attempted.
- **Failure scenario:** A local agent runs the Phase 1 prerequisite against an
  unavailable API and occupies its task budget for many minutes before it can
  report a simple unreachable condition, making the supposedly bounded pilot
  sequence hard to use interactively.
- **Recommended correction:** Add an explicit total preflight/healthcheck
  deadline to the pilot contract. Either pass conservative healthcheck
  timeout/retry environment values from the recipe or extend the healthcheck
  packet with a monotonic overall deadline and a body-free timeout record.

## Explicit no-findings

- **CRE boundary:** No roadmap item directs a change to `cre_collector`,
  `cre_scrapers`, CRE SQL, launchd, EQUIRE configuration, or OM-facts writes.
  The plan repeatedly excludes those surfaces and keeps GetCREdata as the
  production writer ([plan lines 42-57 and 403-413](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L42-L57)).
- **Duplicate generic client:** The plan preserves the official SDK for
  application code and confines local agent additions to the existing helper,
  rather than proposing a second SDK or daemon ([plan lines 24-35 and
  147-160](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L24-L35)).
- **Read-only versus active checks:** The plan correctly separates GET-only
  preflight from the POST-capable healthcheck and smoke matrix, which occur
  only in Phase 1 ([plan lines 103-130](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L103-L130)).
- **Ingress scope:** The plan accurately treats the currently published
  Compose port as a founder-gated ingress decision, not as proof of
  loopback-only isolation ([plan lines 59-62 and 390-401](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L59-L62)).

## Verification and handoff

- Verification was source inspection only: roadmap and forward queue; helper,
  CLI, MCP, healthcheck, capability/smoke scripts; v2 route registration and
  queue-status controller.
- No runtime, Docker, package, Linear, network, CRE, or configuration command
  was run. No source file was edited.
- **Next decision:** Send F1-F5 to a separate skeptic pass. Apply only findings
  the skeptic confirms; preserve any refutations with their evidence.
