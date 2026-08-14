# Final-final independent verification: third AGENTIC-2279 remediation

**Verdict: PASS — no remaining must-fix finding in this remediation.**

Static source inspection and mocked tests only; no Firecrawl host, Docker, `.env`, CRE, Linear, Git write, or external package resolution was run.

## Confirmed: no map or POST-capable CLI probe before the direct idle gate

`run_agent_safe_prerequisites()` now calls `firecrawl_compatibility_doctor.agent_safe_result()` rather than `doctor_result(..., run=True)` (`scripts/firecrawl-ops/firecrawl_request.py:369-446`). `agent_safe_result()` is limited to a loopback root GET preflight, the pinned CLI `--version` probe, and MCP initialize/tools-list (`scripts/firecrawl-ops/firecrawl_compatibility_doctor.py:597-639`); it has no call to `run_cli_probe()`, whose normal operation is the CLI `map` diagnostic.

The direct queue and active-crawl reads still occur immediately before the single recipe POST (`firecrawl_request.py:845-910`). The real-wiring mocked regression proves the prerequisite never calls the map probe and that the direct helper calls are queue GET, active-crawl GET, then `POST /v2/map` (`test_firecrawl_agent_safe.py:543-597`). Its failure-path companion proves an MCP compatibility failure produces no map, no helper HTTP call, and no receipt (`599-642`).

## Confirmed: full normal `doctor --run` remains explicit and separate

The normal `doctor_result(..., run=True)` path still calls `run_cli_probe()` and then MCP (`firecrawl_compatibility_doctor.py:555-594`), while the agent-safe path does not. The operator-only bounded map diagnostic has therefore not been silently weakened or made automatic.

## Confirmed: receipt lifetime and metrics binding

Freshness is enforced at the execution gate for both prerequisite observations (`firecrawl_request.py:845-871`). Receipt construction and validation use nonfuture timestamp validation rather than expiring historical observations (`338-344`, `1187-1195`, `1208-1268`). A request that crosses the execution TTL while writing its terminal record therefore retains its manifest.

The metrics artifact is canonicalized once and validation recomputes both SHA-256 and byte length (`1202-1205`, `1300-1312`). Regressions cover historical receipt validation, digest/metrics tampering, future timestamps, and a clock advance after the execution gate (`test_firecrawl_agent_safe.py:1056-1148`).

## Prior safety controls did not regress

The reviewed source and passing regressions retain proxy-free redirect-rejecting safe transport, exact fixture/PDF and fixed bounds, false/zero/empty-option rejection, inline rather than caller-provided prerequisites, explicit `success is True` and zero idle gating, symlink-resistant receipts, non-2xx rejection including 3xx, body-free finite metrics, and metrics-first/manifest-last atomic terminal receipts. Non-agent-safe output remains routed through its existing normal output paths.

## Verification

Compilation, 24 agent-safe mocked tests, and two mocked compatibility-doctor tests passed. Whitespace validation also passed. The doctor live-loopback suite was intentionally not run.

This is source/mock proof only. It permits the next controlled verification stage under existing CRE and host guardrails; it does not prove local containers, credentials, AI-backed behavior, or production readiness.
