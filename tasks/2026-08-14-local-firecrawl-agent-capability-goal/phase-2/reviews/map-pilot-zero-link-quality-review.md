# AGENTIC-2279 Map Pilot Zero-Link Quality Review

Date: 2026-08-14

## Verdict

**No code or test change is warranted.** The recorded agent-safe map result
(`HTTP 200`, `outcome: success`, `links_count: 0`) is an expected result for
the deliberately minimal `https://example.com/` fixture. It proves the bounded
local map request and its safety/receipt contract, but it is not evidence that
the system can discover a useful site inventory. Those are intentionally
different claims.

## Evidence

- The live receipt is body-free, bound to `limit: 1`, and records an `accept`
  disposition with `links_count: 0`:
  `tasks/agentic-2279/evidence/02d53afd6bd444c19f9c29e8020f39b3-{metrics,receipt}.json`.
- The approved phase-2 design expressly says to accept a successful bounded
  map even when its link count is zero, while treating changed successful
  output as manual review and prohibiting scope expansion/retry
  (`pilot-design-report.md:102-107`).
- The safe helper deliberately projects only the count for a map result
  (`firecrawl_request.py:751-756`) and reserves content-quality thresholds for
  scrape and parse. Its map disposition is therefore `accept` for a valid
  success (`:972-991`); its receipt schema permits a non-negative
  `links_count` (`:1050-1085`). A focused regression already validates a 200
  map response with `links: []` as an accepted valid receipt
  (`test_firecrawl_agent_safe.py:1119-1141`).
- The preceding CLI/MCP validation observed the same fixture at a wider
  `--limit 5` and concluded that its empty same-site list was expected
  (`tasks/2026-08-13-local-firecrawl-rebuild-validation/cli-mcp-validation-report.md:43-45`).
- This follows upstream semantics rather than a local endpoint fault. Map
  results are filtered to the same domain (`apps/api/src/lib/map-utils.ts:315-359`).
  `example.com` is intentionally a one-page documentation domain; the common
  outbound IANA reference is not a same-domain discovery result. With no
  same-site child URL, an empty bounded list is normal.

## Why This Is Not A False Pass

The historical concern about a zero-link map falsely passing applies when a
map pilot is described as a discovery-quality or coverage test. This pilot is
not. It is a strictly bounded, body-free **endpoint and agent-safety canary**:
it verifies loopback routing, authorization gates, request/response shape,
redaction, and terminal receipt handling without retaining a target URL list
or widening work. A synthetic target with a deterministic empty same-domain
inventory is appropriate for that narrow purpose.

Reclassifying this result to `manual_review` or a non-success would make a
healthy minimal canary fail for the fixture's expected behavior, and would
contradict the reviewed pilot contract. It would not improve actual discovery
quality.

## Guardrail For Future Work

Do not generalize this acceptance rule to real discovery jobs. A later
site-inventory or coverage workflow should have a separate, target-specific
quality contract (for example, expected minimum same-domain links, baseline
comparison, or explicit empty-inventory allowance) and must not reuse this
agent-safe pilot receipt as proof of coverage. That would be a separately
authorized design, not a follow-up change to AGENTIC-2279.

## Scope

Read-only assessment only. No live call, Docker, `.env`, API, CRE collector,
Linear, or runtime configuration was changed.
