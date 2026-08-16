# Firecrawl local tools audit closeout (2026-08-16)

**Branch:** fix/firecrawl-local-tools-audit-20260816
**Base:** main at 5113d4c57b04a8ae8af604f34df6ed617449f7e3
**Implementation commit:** 098c73bac (fix: harden local Firecrawl tooling)
**State when written:** local verification complete; PR, CI, and merge pending.

## Goal

Verify the self-hosted Firecrawl API, skills, CLI, MCP wrapper, helper, and
operator documentation, then correct confirmed discrepancies.

## What shipped

- Fixed the browser-session listing configuration gate. With no
  BROWSER_SERVICE_URL, GET /v2/browser now returns the same explicit 503 as
  creation and does not query the database.
- Hardened the smoke matrix so only that explicit browser-service 503 is an
  expected configuration gate; other browser-list failures remain failures.
- Made firecrawl_request.py emit a source-free, deterministic transport failure
  and safe artifacts for connection, timeout, reset, and HTTP transport errors.
- Corrected CLI/skill/reference guidance for unsupported search --pretty,
  legacy /v2/extract, current CLI JSON envelopes, unavailable automatic model
  fallback, and the disabled swarm restart flag. Added regression tests for the
  CLI and swarm documentation claims.
- Resynced canonical Firecrawl skills to the user-level agent skill directory
  and verified the Codex, Claude, and Cursor links resolve to it.

## Verification

- Live local stack healthcheck passed after the API rebuild.
- Full local API smoke matrix passed: 13 of 13 probes.
- Compatibility doctor passed: Firecrawl CLI 1.20.0, MCP 3.24.0, and 27 tools.
- Ops Python suite passed: 89 tests and 112 subtests on the final tree.
- Browser controller harness passed: 2 tests, including TypeScript build.
- py_compile, focused Ruff F-rule lint, and git diff --check passed.

## Adversarial review record

Independent runtime and transport/privacy finder passes found no
merge-blocking issue. A documentation finder raised two candidates:

- **Confirmed and fixed:** a use-cases guide claimed
  --restart-between-stages performs escalation, although the pipeline rejects
  that legacy flag before work starts.
- **Refuted:** the operator handoff still writes the legacy
  MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK key, but current API source does not
  consume it. The revised documentation accurately states that the live API
  does not automatically fall back between Gateway profiles. The unused handoff
  key is deferred technical debt, not a runtime contradiction.

## Decisions

- Return a clear configuration response at the browser boundary rather than
  allowing a disabled optional feature to fall through to a database error.
- Describe only live model-routing behavior as automatic; human-selected
  profiles remain the supported transition mechanism.
- Keep expected optional-service gates visible in smoke output while treating
  unexpected status codes as failures.

## Deliberately deferred

- Configuring optional browser, agent, support, and OCR services is an operator
  environment decision, not part of this audit.
- Removing the inert legacy model-handoff environment key requires a
  compatibility and receipt-restoration migration and is not included here.
