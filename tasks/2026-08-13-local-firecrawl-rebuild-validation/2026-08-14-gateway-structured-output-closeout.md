# Gateway structured-output closeout (2026-08-14)

**Branch:** `fix/gateway-structured-output-fallback`  
**Base:** `main` at `16fd0c90c66ffbfaf12b24a37f7d8fd225458bf8`  
**Commit:** `94622473416a1daa83733aff938327867eb9fa55`  
**PR:** [#32](https://github.com/Agentic-Assets/firecrawl/pull/32) (draft)  
**State:** pushed, reviewed locally, not merged.

## Goal

Make the local OrbStack Firecrawl API use the requested Vercel AI Gateway
models by default and make AI-backed structured output work reliably without
changing the commercial-real-estate collection pipeline.

## What shipped

- The no-argument local profile now selects
  `deepseek/deepseek-v4-flash-0731` at Vercel AI Gateway and configures
  `deepseek/deepseek-v4-pro-0813` as a bounded explicit fallback.
- `extractSmartScrape` accepts a direct provider result only after schema
  validation. It preserves the legacy SmartScrape envelope, handles a user
  schema with a root `extractedData` property, and limits a compatibility
  transaction to one primary request plus one explicit fallback.
- `performSummary` validates a non-empty summary and uses the same bounded
  fallback for invalid or retryable failed primary output. Cost-limit,
  refusal, and non-transient client failures still propagate.
- Compose, profile tooling, local documentation, and synced installed skills
  describe the same Gateway-first contract. `budget` remains an explicit
  single-model OpenRouter alternative.
- Focused API, summary, model-factory, snip, and Python profile regressions
  cover the new behavior.

## Verification

- 40 focused API tests passed.
- `pnpm build` passed. `pnpm knip` completed with its pre-existing
  configuration hint only.
- Profile tests passed: 5 tests and 5 subtests. Scoped Ruff, Python compile,
  Bash syntax, Compose configuration, Prettier, and diff checks passed.
- The API Docker image rebuilt and the OrbStack Compose API was recreated.
  The final health check passed API root plus scrape smoke.
- Live local Gateway checks passed for CLI JSON extraction, summary, query,
  and asynchronous `/v2/extract` on `https://example.com`.
- The EQUIRE `subagent-code-review` process, follow-up summary review, and CRE
  boundary review found no remaining confirmed issue and no protected CRE
  collector, SQL, source-adapter, scheduler, or data-contract change.

## Decisions

- Reused upstream Firecrawl API, SDK, and CLI surfaces. The fork only adds a
  narrow compatibility boundary plus its existing local-ops wrapper; it does
  not add a parallel local client.
- Kept provider responses fail-closed: missing required fields or invalid
  schema output is not fabricated or silently accepted.
- Did not hard-code Gateway pricing for snapshot model aliases because no
  verified provider pricing source was available in this session.

## Deliberately deferred

- The new AI-gated snip typechecks but does not collect in this machine's
  production-mode harness because `TEST_SUITE_WEBSITE` is local. The live
  Compose API validation is the current end-to-end proof.
- Optional Agent beta and browser-action services remain separately gated by
  `EXTRACT_V3_BETA_URL` and `BROWSER_SERVICE_URL`; adding a Gateway key does
  not configure them.

## Left to the operator

- Review and, if ready, explicitly approve merging draft PR #32.
- Decide whether to configure the optional Agent beta and browser services.

