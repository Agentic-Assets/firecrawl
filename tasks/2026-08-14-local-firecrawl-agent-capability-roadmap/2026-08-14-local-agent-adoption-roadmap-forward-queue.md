# Forward queue after the local agent adoption roadmap

Candidate work surfaced during planning. This is a menu, not an approved
roadmap. Verify each item against live Linear and the host runtime before work.

## Hardening

- **Create the read-only capability preflight** (confidence: verified gap, P1)
  Reuse static capability data and GET-only health visibility so agents can
  distinguish unavailable, optional, stale, and unknown capability states
  before they submit work.
- **Generalize bounded waiting by job ID** (confidence: verified gap, P1)
  Extend the helper's crawl waiting contract to batch scrape and extract,
  retaining metrics-only terminal records and nonzero failures.
- **Verify named CLI and MCP versions** (confidence: verified gap, P1)
  Replace unrecorded `@latest` batch dependence with explicit tested package
  versions and a small doctor for CLI contract plus MCP initialize/tools-list.

## Process and docs

- **Create and deduplicate the Linear issue packets** (confidence: blocked
  execution, P1) Restore the `agenticassets` credential first, then update
  existing crawl/PDF issues when their current definition of done owns the
  work. Do not create duplicates from the packet text alone.
- **Publish tested local-agent recipes** (confidence: strong opportunity, P1)
  Sync the selection ladder and bounded research, PDF, map-first, and SDK
  recipes after each has fixture or host-local evidence.
- **Document shared model/OCR handoff** (confidence: verified operational
  risk, P1) Require queue check, exclusive operator window, provider approval,
  recreate/health check, one canary, and deliberate restore/handoff.

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
