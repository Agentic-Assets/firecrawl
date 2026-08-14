# Local Firecrawl Agent Adoption Plan

**Date:** 2026-08-14
**Status:** proposed, no runtime or production change authorized
**Linear project:** Firecrawl Ops & Automation
**Parent context:** [AGENTIC-2253](https://linear.app/agenticassets/issue/AGENTIC-2253/local-firecrawl-api-agent-cli-crawl-polling-parse-quality-and)

## Objective

Make the local Firecrawl stack dependable and easy for local AI agents without
creating a second generic client, a daemon, a scheduler, a tunnel, or a remote
runner. Reuse the upstream API, official SDKs, CLI, and MCP package. Extend
only the thin fork-local operations layer where local agents demonstrably need
bounded polling, preflight, safe receipts, or package compatibility evidence.

The best immediate outcome is an agent that can determine what is available,
select the smallest existing interface, run a bounded task, and hand back a
reproducible result without putting source bodies or secrets in its transcript.

## Evidence and current limits

The existing interface ladder is the starting point.

| Need | Preferred surface | Existing implementation |
| --- | --- | --- |
| Explore tools and one-off operations | MCP wrapper | [firecrawl_mcp.sh](../../../scripts/firecrawl-ops/firecrawl_mcp.sh) |
| Run a normal command | Upstream CLI wrapper | [firecrawl_cli.sh](../../../scripts/firecrawl-ops/firecrawl_cli.sh) |
| Save artifacts, set PDF options, or poll safely | Thin local helper | [firecrawl_request.py](../../../scripts/firecrawl-ops/firecrawl_request.py) |
| Build application automation | Official SDK | [JS SDK](../../../apps/js-sdk) and upstream peer SDKs |
| Diagnose a wire payload | Direct v2 HTTP | Debugging only |

The helper is intentionally not a parallel SDK. It already supplies stable
response envelopes, split artifacts, metrics-only output, positive PDF page
caps, and bounded crawl polling. Application code should use official SDKs.
The CLI and MCP wrappers should remain upstream-owned transport adapters.

This plan is based on source inspection and prior local evidence. The managed
planning environment could not access the OrbStack Docker socket or
`localhost:3002`, so it makes no claim about current runtime health. Each
implementation starts with a host-local, bounded preflight.

## Hard scope

- Local agents on this Mac only. No tunnel, cloud runner, public endpoint, or
  remote-agent access is in scope.
- No generic second client, persistent worker hierarchy, scheduler, database,
  telemetry service, or Supabase write.
- No changes to `cre_collector`, `cre_scrapers`, `cre_pipeline.py`, CRE
  SQL, launchd, EQUIRE environment files, Supabase/Postgres configuration, or
  OM facts. GetCREdata remains the production OM-extraction writer.
- Agents do not autonomously change model or OCR profiles. Those are
  shared-runtime operations that can rewrite the root environment and recreate
  the API.
- Logs and receipts exclude source bodies, raw HTML, headers, cookies,
  credentials, URL query strings, deal data, and client documents.
- A successful HTTP response is not automatically a high-quality document
  result. Preserve current OCR 429, 504, and 422 quality signals.

The client default `localhost` is not an enforceable network-isolation claim:
the shared Compose port is currently published without a loopback host-IP
binding. Any ingress hardening, firewall work, sandbox, or exposure decision is
a separate founder-gated item.

## Interface selection rules

1. Use MCP for interactive discovery when the client has registered the local
   wrapper.
2. Use the upstream CLI for normal interactive scrape, map, parse, and search.
3. Use `firecrawl_request.py` for saved artifacts, advanced PDF controls,
   metrics-only output, or bounded polling. Never use CLI `crawl --wait` for
   local agent automation.
4. Use official SDKs for application code, with explicit API URL, timeout,
   retry, job status, and cancellation lifecycle.
5. Use direct HTTP only when debugging exact request or response behavior.

For any asynchronous task, save the job ID immediately. On uncertain
submission, inspect status or active work before submitting again. Do not retry
by creating duplicate work.

## Phased implementation plan

### Phase 0: establish a truthful local operating contract

Create a read-only preflight from the capability matrix, last recorded smoke
evidence, and `firecrawl_request.py health --metrics-only`. It should emit a
compact versioned JSON document with:

- static route classes, distinct from observed current state;
- API and queue reachability when host-local inspection is available;
- freshness of the most recent smoke evidence;
- required versus absent optional services;
- redacted model-capability status, never environment values;
- resolved CLI and MCP package version when a bounded batch declares one.

Also run `sync_agent_skills.sh --dry-run` before a pilot that depends on an
installed skill, so the local agent sees the intended checked-in guidance.

The preflight performs only static reads and local API GET checks. It makes no
POST, scrape, model call, profile switch, or container mutation. Unknown must
remain unknown.

### Phase 1: adopt three bounded pilots before adding infrastructure

Run these only on a healthy host, with public, authorized, or synthetic input
and caller-selected task artifacts:

First, run `firecrawl_healthcheck.sh --evidence-dir <task-local-dir>` and the
smoke matrix against a bounded public fixture. Those are host-local validation
steps, not preflight: they can perform scrape and other POST requests.

1. **Public scrape and map:** one known public URL, explicit timeout, output
   directory, terminal status, and no source body in the transcript.
2. **Helper crawl submit and poll:** one small public crawl, saved job ID,
   explicit deadline, metrics-only terminal output, and nonzero failure
   behavior.
3. **Born-digital PDF parse:** one public or authorized file, explicit
   `max-pages`, saved Markdown/metadata, and an accept, manual-review, or
   reject disposition.

Until helper receipts are implemented, use one task-local manifest containing
intent, input hash or redacted URL, package version, interface, limits, job ID,
artifact path/checksum, terminal state, and evidence link. It is an execution
artifact, not a durable queue. Linear and Git remain the durable decision and
implementation records.

### Phase 2: close the demonstrated agent ergonomics gaps

Prioritize only the following narrow changes:

1. **Read-only capability preflight** for machine selection.
2. **Bounded wait-job** for existing crawl, batch-scrape, and extract IDs.
3. **Versioned CLI and MCP compatibility doctor** for verified CLI command
   contracts plus MCP initialize and tools-list.
4. **Interface-ladder recipes** in the local agent skill.
5. **Opt-in redacted receipts** when a pilot proves a recurring handoff need.

Every polling request and sleep must stay within the remaining monotonic
deadline. Every terminal failed, cancelled, or timed-out state must exit
nonzero and preserve a body-free terminal record.

### Phase 3: apply model and OCR work only in an exclusive operator window

AI-backed summary, JSON, query, preview, or OCR work is not a per-agent default.
Before a model or OCR profile change:

1. Check the queue and active work.
2. Quiesce or obtain an exclusive operator window.
3. Record the explicit profile, provider-cost approval, and timeout.
4. Change, recreate, and health-check only with authorization.
5. Run one bounded AI or OCR canary.
6. Restore or hand off the shared-runtime state deliberately.

Never automatically retry OCR 429, 504, or 422. Benchmark unfamiliar document
families sequentially with the existing tool and retain `summary.md`,
`qa.json`, and `pages.jsonl`. Promote no parser or profile without
representative evidence and human review.

### Phase 4: defer larger ideas until they solve a demonstrated problem

- **Structured-output provenance sidecar:** only after a separately reviewed,
  merged Gateway structured-output fallback exists and a named workflow needs
  it, expose a redacted receipt for one named workflow:
  request/job ID, retrieval time, schema hash, artifact hash, profile, and
  direct-valid versus fallback status. This is request provenance, not
  unsupported field-level citation.
- **PDF recommendation reader contract:** the existing benchmark already has
  recommendation data. Standardize its schema only if a named consumer needs
  it. Do not create a second recommender or restart OCR merely to read an
  existing result.
- **Validation-only crawl-plan materializer:** reuse params-preview only to
  produce a validated candidate payload. It never launches work and must
  preserve or reduce user caps.
- **Restricted agent sandbox:** make this a founder decision and threat-model
  first. It is not an early default. A future pilot would require a separate
  compose project and unprivileged identity with no repository mount, Docker
  socket, root environment, shared API route, CRE code, CRE secrets, or
  database variables.

## Agent recipes

### Research evidence

Search only discovers candidates. Map a known public hub, choose canonical
primary-source URLs, then scrape selected pages into separate Markdown and
metadata artifacts. Keep URL, retrieval time, and hash beside each derived
claim. RSS or Atom uses native HTTP plus an XML parser, not markdown scraping.

### Public-web discovery

For JS or news hubs, map first and scrape selected pages second. Set domain,
path, page, timeout, and artifact bounds explicitly. This never substitutes for
the governed CRE collector or its source-specific adapters.

### PDF intelligence

Prefer `fast` for dense born-digital text and configured OCR for scanned or
slide-like files. Use the benchmark for unfamiliar families. Treat 429 as
backpressure, 504 as manual review or an explicitly authorized rerun, and 422
as a quality failure that needs a different profile or human review.

### Developer automation

Use an official SDK with `apiUrl: "http://localhost:3002"`, explicit timeout,
retry policy, and submit/status/cancel lifecycle. Use the helper only for
saved split fields, advanced PDF options, or shell-stable bounded polling.

## Linear issue packets

The user requested a separate issue for each idea. The Linear plugin has no
callable tool in this session, and the local `linear` CLI cannot currently
retrieve the `agenticassets` Keychain credential. These are issue-ready
packets, not evidence that a current project inventory has been checked.
Before creation, re-query the project and existing issues to deduplicate
against AGENTIC-2253 and its children. Packets 2 and 8 are update-existing
candidates, not automatically new issues.

### 1. local-agent: add read-only preflight capability contract

- **Priority:** P1
- **Objective:** Emit a versioned JSON decision surface for static route class,
  host-local API and queue observations, prerequisites, and smoke freshness.
- **Definition of done:** Ready, degraded, unavailable, and stale-evidence
  fixtures pass; no non-GET request or runtime mutation occurs; secrets are
  redacted; unknown state is explicit.
- **Out of scope:** Models, app code, optional service enablement, CRE paths,
  and a daemon.

### 2. local-agent: generalize bounded waiting for crawl, batch scrape, and extract

- **Priority:** P1
- **Objective:** Add an allowlisted `wait-job` operation to the existing
  helper for known job IDs only.
- **Definition of done:** Completed, failed, cancelled, timeout, and transient
  mocked states pass for all three job shapes; terminal records omit bodies;
  healthy-host result matches the SDK terminal state.
- **Disposition:** After live issue review, extend AGENTIC-2254 or AGENTIC-2260
  if either still owns the missing job family. Create a new issue only when
  neither definition of done covers it.

### 3. local-agent: pin and diagnose local Firecrawl CLI and MCP packages

- **Priority:** P1
- **Objective:** Verify named CLI and MCP package versions against the local
  API, including one supported CLI command and MCP initialize/tools-list.
- **Definition of done:** Fake-stdio and healthy-host opt-in checks pass;
  package-resolution, API, CLI-contract, protocol, and inventory failures are
  actionable; MCP protocol stdout stays clean.
- **Guard:** Record explicit `FIRECRAWL_CLI_PACKAGE` and
  `FIRECRAWL_MCP_PACKAGE` versions for upgrade testing. Do not silently
  promote an untested `@latest` package.

### 4. local-agent: publish the interface ladder and bounded agent pilots

- **Priority:** P1
- **Objective:** Keep Codex, Claude, Cursor, and shell agents on the right
  existing interface, then prove three bounded pilots.
- **Definition of done:** Each recipe is noninteractive, names inputs and
  artifacts, avoids CLI crawl wait, records package/profile provenance, and
  has fixture or host-local evidence.
- **Out of scope:** Scheduler, cloud access, generic new client, and CRE work.

### 5. local-agent: document exclusive model-profile and OCR-capacity handoff

- **Priority:** P1
- **Objective:** Define a quiesce, change, recreate, health-check, canary, and
  handoff protocol for shared model and OCR profiles.
- **Definition of done:** Queue-aware procedure covers provider approval and
  OCR 429/504/422 behavior; tests or dry-run checks prove profile actions are
  never taken automatically during active work.
- **Out of scope:** New provider, automatic escalation, or OCR retry loop.

### 6. local-agent: add opt-in redacted run receipts

- **Priority:** P2
- **Objective:** Write reproducible handoff evidence without copying content
  into logs.
- **Definition of done:** Secret-injection fixtures prove receipts contain no
  body, headers, key, raw local path, or URL query/userinfo; success, error,
  and timeout results record body-retention as false.
- **Gate:** Agree retention and cleanup before shared persistence.

### 7. local-agent: add a narrow structured-output provenance sidecar

- **Priority:** P2, conditional
- **Objective:** Describe direct-valid, fallback-success, and failed-closed
  structured output for one named workflow.
- **Definition of done:** Source or schema changes change the relevant hashes;
  missing source artifact is labelled request provenance only; AI-gated
  integration proof confirms the additive contract.
- **Dependency:** A separately reviewed, merged Gateway structured-output
  fallback and a named consumer.
- **Founder gate:** Apply Needs Cayman if the retention is product or research
  facing.

### 8. local-agent: standardize consumption of PDF benchmark recommendations

- **Priority:** P2
- **Objective:** Define a versioned reader contract for existing benchmark
  accept, manual-review, reject, mode, profile, and reason fields.
- **Definition of done:** Golden fixtures cover each decision. Reading a prior
  recommendation starts no OCR work and changes no profile.
- **Dependency:** A named consumer and public or licensed canary corpus.
- **Disposition:** After live issue review, update AGENTIC-2256 or AGENTIC-188
  if either owns the quality contract. Create a new issue only for a distinct
  consumer-facing reader contract.

### 9. local-agent: materialize bounded crawl plans without auto-executing them

- **Priority:** P3
- **Objective:** Validate natural-language params-preview output and require a
  separate explicit crawl action.
- **Definition of done:** Unsafe or over-broad plans fail fixtures; the planner
  never posts a crawl; host proof uses preview followed by a distinct
  `limit: 1` crawl.
- **Gate:** Preflight reports approved model capability and budget.

### 10. security: decide whether a restricted local agent sandbox is justified

- **Priority:** P3, decision-gated
- **Objective:** Produce a threat model and go or no-go decision before any
  sandbox build.
- **Definition of done:** Decision covers mounts, secrets, network, resource
  caps, cleanup, and egress tests. A pilot begins only after approval and
  proves no Docker, shared API, root environment, CRE, or database surface is
  reachable.
- **Founder gate:** Needs Cayman plus Human-Signoff.

### 11. security: decide the local Firecrawl ingress posture

- **Priority:** P3, decision-gated
- **Objective:** Decide whether host binding or firewall hardening is needed to
  keep the shared local API local, without creating a tunnel or remote runner.
- **Definition of done:** Read the actual Compose binding, host firewall, and
  intended local clients; record a go or no-go decision, rollback, and proof.
- **Founder gate:** Needs Cayman plus Human-Signoff. No binding or network
  change is authorized by this plan.

## Measures and release gates

Track preflight accuracy, jobs with a saved ID, unknown-outcome and
duplicate-submission rate, p95 duration, schema-validity rate, OCR 422/429/504
rate, receipt completeness, reproducible rerun rate, and exact CLI/MCP package
version.

No proof may create CRE collection, ingest, migration, status activation,
scheduler, cache, or database activity. Use public, licensed, or synthetic
fixtures. Do not commit source bodies, credentials, headers, cookies, deal
data, or client documents.

## Source references

- [firecrawl-local-api skill](../../../.agents/skills/firecrawl-local-api/SKILL.md)
- [firecrawl-ops skill](../../../.agents/skills/firecrawl-ops/SKILL.md)
- [agent tooling reference](../../../docs/firecrawl-ops/references/agent-tooling-firecrawl.md)
- [local capability matrix](../../../docs/firecrawl-ops/references/local-capability-matrix.md)
- [PDF research-agent plan](../../../docs/firecrawl-ops/references/local-pdf-ocr-research-agent-plan.md)
- [ops routing guidance](../../../scripts/firecrawl-ops/CLAUDE.md)
