---
description: "Testing philosophy and execution, focusing on E2E snips and harness usage."
name: "003-testing-strategy"
applyTo: "**"
---

# Testing Strategy

Firecrawl prioritizes End-to-End (E2E) testing over unit testing to ensure real-world reliability.

## E2E Tests (Snips)

The core test suite consists of "snips" located in `apps/api/src/__tests__/snips/`.
- **Naming**: Tests should follow the pattern `feature.test.ts`.
- **Win Conditions**: Every new feature or bug fix MUST have at least:
    - 1 Happy path test.
    - 1+ Failure path tests.

## Using the Test Harness

NEVER try to run tests by starting the API manually with `pnpm start`.
- Use `pnpm harness vitest run <path_to_test>` to run tests (or `pnpm harness pnpm test:snips` for the full snips suite).
- The harness sets up the necessary environment (API server, workers, Redis, etc.).

## Critical Constants

- **scrapeTimeout**: Always import and use `scrapeTimeout` from `./lib` (in the test directory) for all scraping operations in tests.
- Example: `}, 10000 + scrapeTimeout);` for test timeouts (Vitest `testTimeout` in `vitest.config.ts` defaults to 120s for snips).

## Gating Tests

Tests should be gated based on the environment and available keys:
- Requires fire-engine: `!process.env.TEST_SUITE_SELF_HOSTED`
- Requires AI: `!process.env.TEST_SUITE_SELF_HOSTED || process.env.OPENAI_API_KEY || process.env.OLLAMA_BASE_URL`

## API Key Authentication
When `USE_DB_AUTHENTICATION=true` is enabled, tests must use a valid API key from the Supabase `api_keys` table. 
- Default test key format: `fc-<uuid_without_dashes>`.
- Example: `fc-31dba252482749989356775a972cd48a`.

## Test Examples

Refer to existing tests in `apps/api/src/__tests__/snips/v2/` for patterns:
- `@apps/api/src/__tests__/snips/v2/scrape.test.ts`
- `@apps/api/src/__tests__/snips/v2/crawl.test.ts`