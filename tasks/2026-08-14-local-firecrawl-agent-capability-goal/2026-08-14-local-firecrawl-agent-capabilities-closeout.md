# Local Firecrawl agent capabilities closeout (2026-08-14)

**Branch:** `feat/local-firecrawl-agent-capabilities`
**Base:** `origin/main` at `16fd0c90c`
**Implementation commit:** `268559745` (`feat: harden local Firecrawl agent operations`)
**State at writing:** locally committed; branch/PR/Linear evidence follow this capture.

## Goal

Deliver the safe, local-first portions of AGENTIC-2277 through AGENTIC-2284 without changing the CRE collector, database, runtime configuration, or root `.env`.

## Delivered

- A read-only, body-free capability preflight with exact loopback, no-proxy, no-redirect, freshness, and static-contract checks (AGENTIC-2277).
- A pinned CLI/MCP compatibility manifest and bounded doctor, including exact observed-version enforcement and JSONL MCP validation (AGENTIC-2278).
- A deliberately narrow `firecrawl_request.py --agent-safe` pilot surface: exact public fixtures, same-process prerequisites, one bounded request, opaque receipts, and no raw document retention (AGENTIC-2279 and AGENTIC-2281).
- One dry-run-first, human-attested model/OCR handoff surface. Legacy mutable profile and OCR aliases now refuse or delegate; the direct setter cannot write `.env` (AGENTIC-2280).
- Updated skills and onboarding to use Vercel AI Gateway Flash `deepseek/deepseek-v4-flash-0731` with one Pro `deepseek/deepseek-v4-pro-0813` structured-output fallback, and to distinguish optional root `.env` core setup from human-owned AI setup.
- Decision packets only for sandboxing and ingress. No generalized sandbox or LAN/VPN/public ingress was enabled (AGENTIC-2283/2284).

## Verification

- Consolidated local Python suite: `151 passed, 1 skipped, 201 subtests passed`.
- Scoped Ruff check and format check, Python compilation, shell syntax checks, and `git diff --check` passed.
- Independent final adversarial review passed after two symlink/path hardenings; see `final-reviews/whole-branch-adversarial-review-final-addendum.md`.
- Bounded live local evidence was recorded for preflight, pinned doctor, four exact safe pilots, and AI summary/structured-query paths. Receipts under `tasks/agentic-2279/evidence/` are body-free and were independently checked.

## Decisions

- Reused upstream API/SDK/CLI/MCP surfaces instead of creating a second local client. The stdlib helper remains only the controlled agent/advanced-parse seam.
- Treated agent-facing configuration switches as unsafe. A human handoff, not a CLI/MCP/helper flag, owns the narrow allowlisted model/OCR transition.
- Chose exclusive no-follow receipt publication and canonical path checks over trusting caller-provided evidence paths.

## Deliberately deferred

- AGENTIC-2282 remains gated on merged upstream fallback PR #32 and a named consumer.
- AGENTIC-2283 requires Cayman/Human-Signoff before any general sandbox design or implementation.
- AGENTIC-2284 requires Cayman/Human-Signoff before changing the observed local ingress posture.
- Live operator `--apply` transitions are intentionally not exercised: they change `.env`, Docker services, or provider spend and require the documented human attestation.

## Operator follow-up

Push this feature branch, review the draft PR, and decide the three explicit gates above. Do not merge based solely on these local checks.
