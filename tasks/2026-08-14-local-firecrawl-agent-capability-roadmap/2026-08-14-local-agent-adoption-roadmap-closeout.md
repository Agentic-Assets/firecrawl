# Local agent adoption roadmap closeout

**Branch:** `docs/local-firecrawl-agent-roadmap`
**Base:** `origin/main` at `16fd0c90c66ffbfaf12b24a37f7d8fd225458bf8`
**Commit:** `bcbb4bb62de70d5a11e2b528a6f8f8127703e6eb`
**Draft PR:** [#33](https://github.com/Agentic-Assets/firecrawl/pull/33)
**State:** pushed, draft open, merge state `CLEAN` at readback

## Goal

Capture a reuse-first plan for making the self-hosted Firecrawl stack easier
and safer for local AI agents, with a strict commercial-real-estate boundary.

## Shipped

- Added [`local-agent-adoption-plan.md`](../../docs/firecrawl-ops/references/local-agent-adoption-plan.md).
- Defined the existing MCP, CLI, helper, SDK, and direct-HTTP interface ladder.
- Prioritized read-only preflight, bounded job waiting, CLI/MCP compatibility,
  tested recipes, and exclusive model/OCR handoff before larger features.
- Recorded detailed Linear issue packets for all proposed ideas, including
  explicit update-existing rules for crawl and PDF work to avoid duplicates.
- Kept sandbox and ingress work as founder-gated decisions, not default build
  steps.

## Verification

- `git diff --cached --check` passed before commit.
- Agentic Assets voice lint passed on the roadmap.
- Three independent research lanes and two critique rounds reviewed upstream
  reuse, agent ergonomics, prioritization, and CRE isolation. Review findings
  corrected the preflight POST ambiguity, CLI/MCP package scope, OCR retry
  wording, and issue-deduplication language.
- Draft PR readback confirmed the expected head SHA and no configured checks.

## Decisions

- Reuse the upstream API, SDKs, CLI, and MCP package. Do not build another
  generic client or worker system.
- Treat the helper as the shell-only local ergonomics layer.
- Require explicit execution after a crawl-plan preview and explicit operator
  windows for model/OCR profile changes.
- Preserve CRE collector, SQL, Supabase, scheduler, environment, and OM-facts
  isolation. No files in those surfaces changed.

## Deferred and blocked

- Actual Linear issue creation and deduplication are blocked by the missing
  `agenticassets` credential in the local Linear CLI. The requested plugin has
  no callable tool in this session. The roadmap contains issue-ready packets;
  do not claim they were created until project readback succeeds.
- Current local runtime health was not re-proved because the managed session
  cannot access the OrbStack Docker socket or `localhost:3002`.
- No runtime, model, OCR, network, sandbox, or CRE change was attempted.

## Operator handoff

1. Restore the Linear `agenticassets` credential, then re-query Firecrawl Ops
   & Automation and AGENTIC-2253 before creating or updating the packets.
2. Review draft PR #33. Merge remains a founder decision.
3. Before implementing any roadmap item, run a host-local preflight and use
   public, authorized, or synthetic fixtures only.
