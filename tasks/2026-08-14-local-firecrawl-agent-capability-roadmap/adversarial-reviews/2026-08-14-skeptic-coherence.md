# Adversarial Skeptic Verdict: Local Agent Roadmap Coherence

**Date:** 2026-08-14
**Role:** Independent skeptic of F1-F5.
**Scope:** The live roadmap and the sources cited in the finder report only.

## Verdict summary

| Finding | Verdict | Disposition |
| --- | --- | --- |
| F1 | **REFUTED** | Preserve the current fail-closed async-submission rule. |
| F2 | **CONFIRMED** | Pin the normal wrapper path or use one checked-in compatibility manifest. |
| F3 | **REFUTED** | Preserve the Phase 0 static-only contract. |
| F4 | **CONFIRMED** | Make the existing model-profile boundary enforceable before AI-backed agent pilots. |
| F5 | **CONFIRMED** | Give the Phase 1 healthcheck prerequisite an agent-sized overall deadline. |

## Finding-by-finding skeptic pass

### F1 — **REFUTED**

The roadmap already makes re-submission conditional: it says to save the job ID immediately, inspect status or active work before submitting again, and “Do not retry by creating duplicate work” ([roadmap lines 76-78](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L76-L78)). The cited routes confirm that batch-scrape and extract status require a job ID and only crawl has active-work routes ([`v2.ts` lines 255-374](../../../apps/api/src/routes/v2.ts#L255-L374)), but an unavailable inspection path does not authorize the prohibited second submission. An agent that lacks a returned ID must therefore stop rather than retry; the proposed `wait-job` remains correctly limited to known IDs ([roadmap lines 265-272](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L265-L272)).

### F2 — **CONFIRMED**

AGENTIC-2278 requires explicit package versions for upgrade testing and forbids silently promoting an untested `@latest`, but it does not require the normal wrapper defaults to use those tested versions ([roadmap lines 280-292](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L280-L292)). The live CLI and MCP wrappers still default to `firecrawl-cli@latest` and `firecrawl-mcp@latest`, respectively ([`firecrawl_cli.sh` line 6](../../../scripts/firecrawl-ops/firecrawl_cli.sh#L6), [`firecrawl_mcp.sh` line 6](../../../scripts/firecrawl-ops/firecrawl_mcp.sh#L6)); the local-agent skill presents `@latest` as the ordinary path ([skill line 110](../../../.agents/skills/firecrawl-local-api/SKILL.md#L110)).

**Minimal safe correction:** Require AGENTIC-2278 to make normal wrapper defaults read exact tested versions from one checked-in compatibility manifest and print them in diagnostics/receipts; retain `@latest` only as an explicit, visibly labelled upgrade-test override with rollback evidence.

### F3 — **REFUTED**

Phase 0 asks for a resolved CLI/MCP version only “when a bounded batch declares one,” while the same section limits preflight to static reads and local API GET checks and says that unknown must remain unknown ([roadmap lines 103-121](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L103-L121)). An exact declared `package@version` can be reported from static configuration, and an unbounded `@latest` is not a declared immutable version; the roadmap does not require `npx` or registry resolution during preflight. The fact that today’s wrappers use `npx -y` ([`firecrawl_cli.sh` line 109](../../../scripts/firecrawl-ops/firecrawl_cli.sh#L109), [`firecrawl_mcp.sh` line 11](../../../scripts/firecrawl-ops/firecrawl_mcp.sh#L11)) therefore does not establish the claimed Phase 0 conflict.

### F4 — **CONFIRMED**

The roadmap correctly classifies profile changes as shared-runtime operations and requires an exclusive authorized operator window ([roadmap lines 51-53 and 162-172](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L51-L53)), but the selected helper still exposes `--model-profile` to every command ([`firecrawl_request.py` lines 405-419](../../../scripts/firecrawl-ops/firecrawl_request.py#L405-L419)). Supplying that flag directly runs `set_model_profile.sh` and normally recreates the API ([`firecrawl_request.py` lines 143-161](../../../scripts/firecrawl-ops/firecrawl_request.py#L143-L161)); the CLI wrapper exposes the equivalent path ([`firecrawl_cli.sh` lines 15-28 and 96-106](../../../scripts/firecrawl-ops/firecrawl_cli.sh#L15-L28)). The policy is consequently behavioral rather than enforced at the agent-facing interface.

**Minimal safe correction:** Make AGENTIC-2280 a prerequisite for AI-backed agent pilots and require it to choose an enforceable operator-only profile path or a narrowly validated authorization gate; until that proof exists, recipes and fixtures must reject profile-changing flags.

### F5 — **CONFIRMED**

Phase 1 mandates `firecrawl_healthcheck.sh` before its pilots, but only the later scrape/map pilot has an explicit timeout and the healthcheck step has no overall deadline ([roadmap lines 123-139](../../../docs/firecrawl-ops/references/local-agent-adoption-plan.md#L123-L139)). The current healthcheck defaults to 12 root-check attempts at up to 90 seconds each with 2-second inter-attempt sleeps ([`firecrawl_healthcheck.sh` lines 72-75 and 100-111](../../../scripts/firecrawl-ops/firecrawl_healthcheck.sh#L72-L75)), allowing about 18 minutes before its subsequent smoke request. This is not an agent-sized bounded prerequisite when the API is unavailable.

**Minimal safe correction:** Add a Phase 1 total preflight deadline and require the pilot recipe to pass conservative healthcheck timeout/retry environment values; any future helper change should preserve a monotonic overall deadline and a body-free timeout record.

## Verification and next decision

Verification was source inspection only: the live roadmap and forward queue, `v2.ts`, `queue-status.ts`, CLI/MCP wrappers, the local-agent skill, `firecrawl_request.py`, and `firecrawl_healthcheck.sh`. No roadmap, Linear issue, runtime, Docker configuration, package, or CRE surface was changed or exercised.

**Next decision:** Apply only the minimal corrections for F2, F4, and F5 to the appropriate roadmap or issue packets; retain the F1 and F3 refutations as review evidence, then rerun a focused skeptic pass on the revised text.
