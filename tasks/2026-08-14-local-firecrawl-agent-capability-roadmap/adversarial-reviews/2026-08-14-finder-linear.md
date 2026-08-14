# Adversarial finder report: Linear packets and execution plan

**Date:** 2026-08-14
**Role:** finder pass only, not a skeptic verdict
**Scope:** the local-agent adoption plan, its forward queue, and live read-only
Linear records for AGENTIC-2253, AGENTIC-2260, AGENTIC-188, AGENTIC-195, and
AGENTIC-2277 through AGENTIC-2284.
**Authority:** no Linear, runtime, model, OCR, network, or CRE change was made.

## Method and evidence

I used the adversarial-review finder pattern: look for discrete, falsifiable
planning and tracking defects rather than judging the overall direction. I
read the roadmap and forward queue at draft-PR #33 head, inspected the current
wrapper and helper implementation, and used authenticated **read-only** Linear
views. The live records were all Backlog and unassigned at inspection.

Every item below is a candidate finding for an independent skeptic. An exact
change is deliberately stated so it can be confirmed or rejected without
guesswork.

## Candidate findings

### F-LIN-01 — P1 execution order is not represented by the live priority or dependency data

- **Severity:** High
- **Affected packets:** AGENTIC-2277, AGENTIC-2260, AGENTIC-2278,
  AGENTIC-2279, AGENTIC-2280, and conditionally AGENTIC-2282/AGENTIC-188/
  AGENTIC-195.
- **Evidence:** The forward queue prescribes `2277 -> 2260 -> 2278 -> 2279 ->
  2280` at `2026-08-14-local-agent-adoption-roadmap-forward-queue.md:21-25`.
  Live Linear priorities instead mark 2277-2280 as Urgent, 2260 as Medium,
  2281/2282/188/195 as High, and 2283/2284 as Medium. The issue descriptions
  name several preconditions, but none records the P1 chain as a blocker
  relationship. In particular, 2279 can be selected as Urgent before the
  version doctor and generalized terminal-status contract it is intended to
  demonstrate.
- **Failure scenario:** An executor triages by Linear priority, starts a pilot
  or model/OCR handoff before preflight, package compatibility, or status
  normalization is available, and produces evidence that cannot be compared or
  safely repeated.
- **Exact change:** Make the sequence machine-readable in Linear: add explicit
  blocker/blocked-by relations (or, if the project deliberately does not use
  relations, an identical `## Prerequisites` section in each packet) for
  `2277 -> 2260 -> 2278 -> 2279`; make 2280 depend on a successful healthy-host
  pilot rather than merely sharing P1. Reconcile the live priorities with that
  order, especially 2260's Medium priority. Add the exact same dependency map
  to the roadmap's packet table so the documents and work ledger cannot drift.

### F-LIN-02 — AGENTIC-2278 promises a pin while both agent wrappers still default to mutable `@latest`

- **Severity:** High
- **Affected packet:** AGENTIC-2278.
- **Evidence:** The plan says not to silently promote an untested latest
  package (`local-agent-adoption-plan.md:280-292`) and the forward queue says
  to replace unrecorded `@latest` batch dependence (forward queue:15-17).
  The actual defaults remain `firecrawl-cli@latest` in
  `scripts/firecrawl-ops/firecrawl_cli.sh:5-7` and `firecrawl-mcp@latest` in
  `scripts/firecrawl-ops/firecrawl_mcp.sh:4-7`. The documented package
  overrides are optional, so a normal agent invocation still downloads a new,
  untested version.
- **Failure scenario:** The doctor passes for a package version today. A later
  normal invocation resolves a changed `@latest`, changing command flags or
  MCP protocol behavior without a code or config diff, and the recorded
  evidence falsely appears reproducible.
- **Exact change:** In 2278, require a single explicit tested default for each
  wrapper (exact `name@version`, chosen by the doctor) and tests that fail if a
  default or automation recipe resolves `@latest`. Treat an upgrade as a
  deliberate doctor run that updates that tested default and its evidence.
  Update the skills and examples that currently invoke bare `npx ...@latest`
  to use the wrapper or the same exact version. Keep a caller override only as
  an explicit upgrade-test path, not the normal automation path.

### F-LIN-03 — AGENTIC-2280's non-disruption acceptance criterion is not enforceable by the named tools

- **Severity:** Critical
- **Affected packet:** AGENTIC-2280; also the plan's Phase 3 contract.
- **Evidence:** The plan requires a queue check, exclusive window, and
  authorization before profile changes (`local-agent-adoption-plan.md:162-172`)
  and says profile actions must never occur automatically during active work
  (`:307-318`). In current code, every helper subcommand accepts
  `--model-profile` (`firecrawl_request.py:405-419`), whose implementation
  immediately runs `set_model_profile.sh` and recreates the API
  (`:143-161`). The CLI wrapper has the same unguarded path
  (`firecrawl_cli.sh:65-106`). `set_model_profile.sh` writes root `.env`
  before any queue or active-crawl check (`set_model_profile.sh:88-141`).
- **Failure scenario:** A local agent appends `--model-profile gateway-pro` to
  a harmless scrape or health command. It changes shared provider/model state
  and recreates the API while a crawl or OCR task is active, despite the
  procedure's stated prohibition.
- **Exact change:** Make 2280 choose and test one enforceable operator-only
  profile-change path. At minimum, profile mutation must first perform bounded
  queue and active-work checks, fail closed on active or unknown state, and
  require an explicit operator authorization record before writing `.env` or
  recreating `api`. Remove or reject profile flags from ordinary agent-facing
  helper/CLI command paths until that guard exists. The acceptance test must
  prove that an active-work or unavailable-status fixture leaves `.env` byte
  identical and never calls Compose; a prose-only dry run is insufficient.

### F-LIN-04 — AGENTIC-2277 does not define the preflight schema or freshness policy needed for deterministic decisions

- **Severity:** Medium
- **Affected packet:** AGENTIC-2277.
- **Evidence:** Phase 0 requests a “compact versioned JSON document” and
  “freshness of the most recent smoke evidence”
  (`local-agent-adoption-plan.md:103-121`). Its packet asks fixtures to cover
  ready, degraded, unavailable, and stale (`:251-263`), but defines neither
  an output schema/status algebra, an evidence timestamp source, nor a stale
  threshold. The live issue repeats the same unspecified terms.
- **Failure scenario:** Two implementations both pass their own stale fixture
  while one treats a missing timestamp as fresh and another treats it as
  unavailable. Downstream agents cannot safely compare capability results or
  decide whether a POST pilot is allowed.
- **Exact change:** Add a small versioned JSON Schema and status table to 2277:
  required `schema_version`, `observed_at`, `source/evidence_digest`,
  per-check `observed|static|unknown` origin, and closed enum for
  `ready|degraded|unavailable|stale|unknown`. Set a named,
  caller-visible max-evidence-age input with a safe default; missing or
  unparsable evidence must be `unknown`, not fresh. Require deterministic
  clock fixtures and JSON-schema validation in the definition of done.

### F-LIN-05 — AGENTIC-2260 lacks a route-specific terminal-state contract for the new job families

- **Severity:** High
- **Affected packet:** AGENTIC-2260.
- **Evidence:** The existing helper only polls the crawl route using a global
  `{"completed", "failed", "cancelled"}` terminal-state set
  (`firecrawl_request.py:30-31,639-724`). 2260 expands that behavior to
  `/v2/batch/scrape/:id` and `/v2/extract/:id`, but its definition of done
  requires generic mocked terminal states only (`local-agent-adoption-plan.md:265-278`).
  The capability matrix explicitly marks both status variants as only partly
  covered (`local-capability-matrix.md:13-16,33-34`).
- **Failure scenario:** A convenient mock gives all three routes crawl-shaped
  status values, while a real batch or extract terminal response uses a
  different shape or terminal label. The helper keeps polling until deadline,
  or reports a failed job as successful.
- **Exact change:** Before implementation, add a route table to 2260 defining
  the authoritative status endpoint, id field, terminal and failure states,
  and body-free terminal projection for each of crawl, batch scrape, and
  extract. Capture one sanitized healthy-host response per route or cite an
  official SDK/API contract. Require unknown terminal values to fail closed,
  and use those exact response fixtures rather than a shared crawl-only mock.

### F-LIN-06 — The receipt contract contradicts its own raw-path prohibition

- **Severity:** Medium
- **Affected packet:** AGENTIC-2281 and the Phase 1 temporary manifest.
- **Evidence:** The Phase 1 manifest contains an “artifact path/checksum”
  (`local-agent-adoption-plan.md:141-145`). AGENTIC-2281 similarly records an
  “artifact checksum/path,” but immediately prohibits a raw local path
  (`:320-331`; same wording in the live issue). The forward queue correctly
  says receipts must contain no raw paths (lines 35-37), but none of the three
  locations specifies the permitted replacement.
- **Failure scenario:** An implementation stores `/Users/.../task/artifact.md`
  to make the receipt reproducible. It passes an overly narrow secret fixture
  but violates the hard scope and leaks local project or client directory
  structure in a shared handoff.
- **Exact change:** Replace all three “path” fields with a precise,
  non-sensitive `artifact_ref` contract: checksum plus an opaque artifact ID
  and, if necessary, a controlled relative name inside a caller-declared
  task-local directory. Explicitly prohibit absolute paths and parent
  traversal; test both. State where the owner resolves `artifact_ref` locally
  and that the resolution map is not persisted with the receipt.

### F-LIN-07 — Conditional work has prerequisites but no concrete gate evidence, and the parent still advertises superseded swarm work

- **Severity:** Medium
- **Affected packets:** AGENTIC-2253, AGENTIC-2282, AGENTIC-188, and
  AGENTIC-195.
- **Evidence:** 2282 requires a separately reviewed **merged** Gateway fallback
  and a named consumer (`local-agent-adoption-plan.md:333-346`), while 188
  requires a named consumer and licensed/public corpus (`:348-361`). Neither
  issue identifies the consuming workflow, the required merged SHA/PR, or a
  readiness receipt, so “conditional design” can be mistaken for authorized
  implementation. Separately, the live AGENTIC-2253 description still links
  AGENTIC-195 as “crawl swarm ... blocked on crawl agent-safety,” while live
  AGENTIC-195 is now the validation-only materializer and the roadmap
  expressly forbids reviving a swarm.
- **Failure scenario:** An executor starts a provenance sidecar without a
  consumer or treats an old parent description as permission to reintroduce a
  scheduler or autonomous crawl loop.
- **Exact change:** Add a non-empty `## Start gate` to 2282 and 188 with named
  consumer, source/retention class, prerequisite PR URL and merged SHA (where
  applicable), and a founder decision reference if product/research facing.
  Until every field is supplied, state “design only, no implementation.” Add a
  “current child dispositions” note to AGENTIC-2253 that supersedes its
  historical AGENTIC-195 swarm wording and points to the validation-only
  contract. Do not rewrite the original 2026-08-13 evaluation evidence.

### F-LIN-08 — AGENTIC-188's canary can silently rely on a still-open page-cap defect tracker

- **Severity:** Medium
- **Affected packet:** AGENTIC-188; related AGENTIC-2262.
- **Evidence:** AGENTIC-188 requires a public/licensed full-PDF canary and
  recommendation reader contract, while the plan calls for explicit
  `max-pages` in the PDF pilot (`local-agent-adoption-plan.md:137-139,
  348-361`). AGENTIC-2262 remains a Backlog child whose title says the page
  cap reports eight pages while returning the full PDF. The parent comments
  contain implementation evidence of a fix, but the issue has no reconciled
  tracker disposition and 188 does not name a page-count assertion.
- **Failure scenario:** The PDF benchmark accepts a recommendation based on
  metadata claiming a bounded parse, but a regression processes or stores the
  full document. This can distort quality, runtime, and resource evidence
  without a failed reader-contract test.
- **Exact change:** Add a dependency/audit gate to AGENTIC-188: either cite a
  current-main verification of AGENTIC-2262 and reconcile that tracker, or
  include an integration fixture asserting both `numPages` and content/page
  artifact bounds for a capped public PDF. The reader must reject a benchmark
  record whose declared page cap and observed artifact metrics conflict.

### F-LIN-09 — AGENTIC-195 has no numerical policy caps, so “preserve caller caps” permits an over-broad candidate

- **Severity:** Medium
- **Affected packet:** AGENTIC-195.
- **Evidence:** The issue validates domain, path, depth, page, concurrency,
  timeout, and artifact caps, but only promises to preserve or reduce whatever
  the caller supplied. The roadmap repeats that rule at
  `local-agent-adoption-plan.md:363-374`. The preflight only reports redacted
  model capability; it does not establish a maximum cost, page count, or
  allowed host policy. `params-preview` is an LLM-backed POST route
  (`local-capability-matrix.md:32`).
- **Failure scenario:** A caller supplies an enormous but technically explicit
  cap. The materializer faithfully preserves it, creates a convincing-looking
  candidate, and a later “separate” execution turns it into an expensive or
  unsafe crawl.
- **Exact change:** Define a static local policy with explicit maxima for pages,
  depth, concurrency, timeout, artifact size, and permitted public host
  classes. Validate both caller input and model preview against it; reject
  private, link-local, loopback, metadata, and cap-expanding targets. Make the
  host proof use the policy's smallest limit and prove the materializer itself
  performs only `/params-preview`, never `/crawl`.

### F-LIN-10 — Sandbox decision gate is circular and ingress gate is ambiguous about permitted discovery

- **Severity:** Medium
- **Affected packets:** AGENTIC-2283 and AGENTIC-2284.
- **Evidence:** 2283's objective is to produce a threat model **before** a
  go/no-go decision, yet it says to use `codex-security:threat-model` “only
  after approval” (`local-agent-adoption-plan.md:376-388`). 2284 similarly
  requires Compose/firewall/reachability evidence to make a decision but has
  `Needs Cayman` plus `Human-Signoff` without distinguishing passive evidence
  gathering from a network change (`:390-401`).
- **Failure scenario:** Work stops because the evidence required for a founder
  decision is itself treated as prohibited, or an executor interprets a
  decision label as permission to run a live non-host reachability probe before
  scope and safety are approved.
- **Exact change:** Split each packet into two explicit gates: (1) permitted
  read-only/static evidence and a recommendation, with no build/binding/firewall
  mutation; (2) founder-approved implementation or dynamic pilot. For 2283,
  allow the threat model and static Compose review in gate 1, but retain
  `Needs Cayman`/`Human-Signoff` for a sandbox build. For 2284, enumerate the
  exact read-only host and non-host probes permitted in gate 1 and require a
  founder-approved test plan before any probe that can leave the host.

## Explicit no-findings / confirmed strengths

| Review target | No-finding result |
| --- | --- |
| Parentage and deduplication | Live Linear confirms that 2277-2284 and 2260 are children of AGENTIC-2253. AGENTIC-188 and AGENTIC-195 remain intentionally preserved existing packets, not duplicate new children. |
| Governance labels | Read-only project inventory confirms `Agentic-Assets/firecrawl`, `Local`, `agent:local`, and `DoD:Set` on the new/updated roadmap packets. 2282, 2283, and 2284 additionally retain `Needs Cayman` and `Human-Signoff`. No assignment, state, or routing label was changed in this pass. |
| CRE protection | Every reviewed packet explicitly excludes the CRE collector, CRE SQL, launchd, root environment, database, and OM-facts production surfaces. No proposed packet authorizes commercial-listing collection or data writes. |
| Interface reuse | The plan consistently favors the upstream CLI/MCP/SDK and the existing thin helper, with no new generic client, daemon, scheduler, remote runner, tunnel, or telemetry service. |
| Bounded-polling intent | AGENTIC-2260 explicitly preserves saved IDs, monotonic deadlines, nonzero terminal failure, and body-free terminal evidence. The gap in F-LIN-05 is a missing per-route normalization contract, not a rejection of that design. |
| Model/OCR restraint | The plan correctly treats AI/OCR profiles as shared runtime rather than per-agent defaults, preserves 429/504/422 behavior, and prohibits automatic retry. F-LIN-03 identifies the currently missing enforcement path. |

## Review disposition and next decision

This finder pass identified ten falsifiable candidates. The highest-value
skeptic checks are F-LIN-02 (whether existing `@latest` behavior really defeats
the stated pin), F-LIN-03 (whether profile mutations can bypass queue checks),
F-LIN-05 (whether the three status routes have materially different contracts),
and F-LIN-10 (whether the decision gates are truly circular under the canonical
label taxonomy). Confirm only findings that survive that independent review,
then update the roadmap and affected Linear descriptions/relations together in
one narrow tracking pass. No runtime change should be bundled with those
documentation and issue-tracking corrections.

## Work, verification, blockers

- **Work performed:** Reviewed all eleven roadmap packets and their plan/queue
  mapping; inspected the relevant helper and wrapper paths; compared the named
  issues to live Linear read-only records.
- **Verification:** Confirmed live child relationships, Backlog/unassigned
  state, priorities, and required control labels through the host-authenticated
  Linear CLI. Confirmed current wrapper defaults and profile mutation behavior
  by source inspection.
- **Blockers:** None for the finder report. Runtime health was intentionally
  not asserted or probed.
- **Next decision:** Run an independent skeptic pass over F-LIN-01 through
  F-LIN-10 before changing Linear priorities, relations, descriptions, labels,
  or repository documents.
