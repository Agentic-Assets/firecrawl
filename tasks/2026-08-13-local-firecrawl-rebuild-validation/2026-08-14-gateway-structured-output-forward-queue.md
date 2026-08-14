# Forward queue after Gateway structured-output hardening (2026-08-14)

Candidate work surfaced during the Gateway validation. This is a menu, not a
roadmap; each item needs fresh verification before action.

## Hardening

- **Make the AI JSON snip collect in the local harness** (priority: P1,
  confidence: verified gap)
  Supply a harness-safe non-local test-site configuration or a documented
  self-hosted invocation. The test typechecks and live Compose validation
  passes, but the current production-mode harness rejects its local
  `TEST_SUITE_WEBSITE` before collecting tests.

- **Add a post-provider-output integration fixture** (priority: P2,
  confidence: verified opportunity)
  Preserve a recorded, non-sensitive direct-schema result and schema-marker
  result at the OpenAI-compatible boundary. This would cover the Vercel
  response shape without a live provider call in every test run.

## Robustness

- **Verify snapshot-model pricing metadata with a primary provider source**
  (priority: P2, confidence: verified observability gap)
  The local extract worker completes successfully but lacks pricing metadata
  for the requested snapshot aliases. Do not hard-code a price until Vercel
  Gateway publishes a stable authoritative mapping.

- **Exercise optional service gates separately** (priority: P3,
  confidence: verified configuration gate)
  Agent beta and browser actions need their own service URLs. Treat them as
  separate deployment decisions rather than Vercel Gateway failures.

## Simplification

- **Evaluate a shared structured-output recovery helper** (priority: P3,
  confidence: hypothesis)
  JSON extraction and summary now share the same policy but remain in distinct
  upstream transformer flows. Consolidate only if a third flow needs the same
  bounded explicit fallback; avoid a premature generic abstraction.

## Process

- **Address the existing Knip configuration hint** (priority: P3,
  confidence: pre-existing hint)
  `src/lib/threat-protection/types.ts` remains ignored in `knip.config.ts`.
  Verify whether that ignore is still necessary before removing it; it is
  unrelated to the Gateway work.

