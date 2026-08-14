# Forward queue after local Firecrawl agent capabilities (2026-08-14)

Candidate work surfaced during this implementation. This is a prioritized menu, not an execution roadmap; verify each item before acting.

## Hardening

- **Resolve the AGENTIC-2282 external gate** (confidence: verified dependency; priority: P1)
  Merge the upstream structured-output fallback only after its upstream review, then name a local consumer and add a gated end-to-end schema-invalid fallback proof. Do not add a parallel local JSON fallback.

- **Decide local ingress posture explicitly** (confidence: verified current decision gate; priority: P1)
  The current Compose API publication needs a Cayman/Human-Signoff decision before any LAN, VPN, or public exposure change. Preserve loopback-only agent-safe commands until then.

- **Turn receipt schema checks into a CI-required gate** (confidence: verified implementation; priority: P2)
  The safe-pilot and operator receipts now have strict tests. Add a dedicated CI selection once the fork's CI policy identifies the appropriate operations lane, so future wrapper edits cannot omit path/no-follow coverage.

## Simplification

- **Consolidate repeated loopback transport helpers** (confidence: likely duplication; priority: P2)
  The preflight, compatibility doctor, safe helper, and operator handoff each need no-proxy/no-redirect transport. Extract only a small, testable shared utility after checking that it does not blur their distinct read-only versus mutation authorities.

- **Archive superseded mutable-operation guidance** (confidence: verified documentation migration; priority: P3)
  After the draft PR is accepted, identify historical reports that still describe retired mutable flags, label them historical, and avoid preserving them as executable examples.

## Evaluation

- **Add a scheduled, read-only capability snapshot** (confidence: useful but unimplemented; priority: P2)
  Run only the preflight and static compatibility doctor on a human-approved cadence, retaining body-free results and alerting on a pinned-version or base-HTTP regression. Do not turn it into an automatic Docker/model repairer.

- **Add representative non-public-free fixture coverage behind explicit authority** (confidence: design hypothesis; priority: P3)
  The exact `example.com` pilots intentionally prioritize safety over discovery quality. Broader agent workload validation needs a separately approved fixture/retention contract, not looser safe-mode URL or file parsing.
