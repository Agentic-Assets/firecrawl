# Forward queue after the local agent adoption roadmap

Candidate work surfaced during planning. This is a menu, not an approved
roadmap. Verify each item against live Linear and the host runtime before work.

## Hardening

- **Create the read-only capability preflight** (confidence: verified gap, P1)
  Reuse static capability data and GET-only health visibility so agents can
  distinguish base HTTP, async jobs, CLI/MCP, AI formats, and PDF OCR states
  before they submit work. Require an explicit capability and fail closed on
  unknown, stale, or unavailable evidence.
- **Generalize bounded waiting by job ID** (confidence: verified gap, P1)
  Extend the helper's crawl waiting contract to batch scrape, retaining
  route-specific terminal rules, metrics-only terminal records, nonzero
  failures, and a crawl idempotency key. Do not expand new workflows around
  deprecated v2 extract without a named legacy consumer.
- **Verify named CLI and MCP versions** (confidence: verified gap, P1)
  Replace unrecorded `@latest` batch dependence with exact checked-in package
  defaults and a small doctor for CLI contract plus MCP initialize/tools-list.
  Treat `@latest` as a visibly labelled human upgrade probe with rollback
  evidence.

## Process and docs

- **Run the P1 packets in interface-ladder order** (confidence: live-deduped
  plan, P1) Start with AGENTIC-2277 preflight, then AGENTIC-2260 bounded waits,
  AGENTIC-2278 package doctor, AGENTIC-2279 pilots, and AGENTIC-2280 handoff.
  Reconfirm host runtime before each item; do not confuse a planning packet
  with proof that the shared stack is healthy.
- **Publish tested local-agent recipes** (confidence: strong opportunity, P1)
  Sync the selection ladder and bounded research, PDF, map-first, and SDK
  recipes after each has fixture or host-local evidence. Agent-safe wrapper
  recipes reject remote endpoints and profile-changing flags before work, and
  crawl pilots require explicit small caps.
- **Document shared model/OCR handoff** (confidence: verified operational
  risk, P1) Require queue check, exclusive operator window, provider approval,
  recreate/health check, one canary, and deliberate restore/handoff. It must
  be an operator-only mutation path with tests proving agent flags cannot write
  `.env` or recreate the API.

## Robustness

- **Add opt-in redacted helper receipts** (confidence: strong opportunity,
  P2) Capture job ID, limits, status, artifact hashes, and error class without
  source bodies, headers, keys, raw paths, or query strings.
- **Standardize PDF recommendation consumption only for a named consumer**
  (confidence: hypothesis, P2) The benchmark already emits recommendation
  information. Add a versioned reader contract rather than a second recommender.

## Capability

- **Add structured-output provenance only after a merged fallback and named
  consumer** (confidence: conditional, P2) Keep it additive and redacted;
  do not imply field-level citation where only request provenance exists.
- **Validate crawl plans without executing them** (confidence: strong
  opportunity, P3) Preserve user caps and require a separate explicit crawl.

## Founder decisions

- **Decide the local ingress posture** (confidence: verified architectural
  question, P3) Current client defaults do not prove loopback-only network
  isolation. Any binding, firewall, tunnel, or exposure action needs founder
  approval.
- **Decide whether a restricted agent sandbox is justified** (confidence:
  hypothesis, P3) Require a threat model and evidence from the simpler
  preflight/receipt work before adding host complexity.
