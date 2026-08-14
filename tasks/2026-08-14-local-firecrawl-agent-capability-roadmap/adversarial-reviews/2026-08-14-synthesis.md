# Adversarial review synthesis: local Firecrawl agent roadmap

**Date:** 2026-08-14
**Scope:** roadmap, forward queue, model-routing reference, and the eleven
mapped Firecrawl Ops & Automation issues.
**Authority:** documentation and Linear-tracking improvements only. No runtime,
Docker, provider, ingress, root environment, CRE collector, SQL, or database
action is authorized by this synthesis.

## Review method

Four independent finder passes examined coherence, agent operations, Linear
packets, and user value. Separate skeptic passes then tried to refute each
candidate against primary checked-in sources and read-only Linear records.
Only confirmed findings informed the update below. Individual reports remain
beside this synthesis.

## Confirmed changes applied

1. **Pin normal agent execution.** AGENTIC-2278 now requires one exact
   checked-in CLI/MCP compatibility manifest for ordinary wrapper calls;
   `@latest` is a labelled human upgrade probe, not the default path.
2. **Keep the helper small and current.** AGENTIC-2260 initially covers crawl
   and batch-scrape status only, with route-specific terminal contracts. New
   structured work uses v2 scrape JSON; deprecated v2 extract waiting requires
   a named legacy consumer. Crawl idempotency can prevent a second job but
   cannot recover a missing job ID.
3. **Make the agent boundary enforceable.** Agent-safe wrapper recipes must
   reject non-loopback origins, profile-changing flags, and unbounded crawl
   inputs before any HTTP, npm, Docker, or environment mutation. Profile work
   remains operator-only and must fail closed when queue/active state is
   unknown or active.
4. **Make preflight honest.** AGENTIC-2277 now has a per-capability, versioned
   status contract with provenance, freshness handling, and a fail-closed
   `--require` mode. Basic GET health cannot claim AI, OCR, browser, or agent
   readiness.
5. **Bound the pilots.** The healthcheck prerequisite receives a host-level
   outer deadline plus conservative retry settings. The first crawl proof is
   explicitly small and scoped (`limit: 1`, `maxConcurrency: 1`).
6. **Remove receipt ambiguity.** Manifests and future receipts use a defined
   `artifact_ref`, never an unrestricted persisted path.
7. **Keep model documentation literal.** The routing reference now matches the
   configured Gateway Flash 0731 primary and Pro 0813 structured-output
   fallback, and makes manual escalation an operator operation.
8. **Make P1 decisions measurable.** A compact five-packet scorecard defines
   denominator, pass rule, body-free evidence, and the next decision without
   adding telemetry.
9. **Limit validation-only planning.** AGENTIC-195 now needs static numerical
   caps and public-host policy validation in addition to preserving caller
   limits.
10. **Correct historical parent context.** AGENTIC-2253 retains its original
    evaluation evidence but marks its former crawl-swarm reference as
    historical and links AGENTIC-195's validation-only replacement.

## Explicitly refuted or deferred ideas

- Do not add a second generic client, daemon, scheduler, remote runner,
  telemetry service, or CRE integration.
- Do not turn the suggested interface-ladder order into Linear blocker
  relations or change its priorities merely to encode a sequence.
- Do not expand helper waiting around v2 extract without a named legacy
  consumer.
- Do not treat an empty public-web map result as a failed route-health smoke,
  or add language-specific SDK watcher guidance without a named consumer.
- Do not add AGENTIC-2262 as a prerequisite to AGENTIC-188, or change the
  sandbox/ingress founder-gate design. No live ingress discovery or mutation
  is authorized here.
- Do not impose task-local receipt retention policy beyond the now-defined
  artifact reference without a separately scoped privacy decision.

## Evidence artifacts

- `2026-08-14-finder-coherence.md` and `2026-08-14-skeptic-coherence.md`
- `2026-08-14-finder-agentops.md` and `2026-08-14-skeptic-agentops.md`
- `2026-08-14-finder-linear.md` and `2026-08-14-skeptic-linear.md`
- `2026-08-14-finder-value.md` and `2026-08-14-skeptic-value.md`

## Next decision

Use the updated issue definitions as the source of truth for future narrow
implementation branches. Before the first implementation, re-read the live
issue and the host-local runtime state; a documentation decision is not proof
that OrbStack, the local API, or an AI provider is currently ready.
