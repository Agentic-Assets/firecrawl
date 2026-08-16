# Forward queue after Firecrawl local tools audit (2026-08-16)

Candidate work surfaced during the audit. This is a menu, not a roadmap; verify
each item before scheduling it.

## Hardening

- **Migrate the inert structured-output fallback handoff key**
  *(confidence: verified technical debt; priority: medium)*
  Remove MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK from future model handoffs only
  after adding compatibility handling for historic receipts and existing root
  .env files. The live API source does not consume the key, but old receipts may
  include it.

- **Run the local smoke matrix in a controlled CI lane**
  *(confidence: verified gap; priority: medium)*
  Preserve the present manual runtime proof with a self-hosted, configuration
  aware CI job or nightly host check. Optional service gates must remain
  explicit rather than becoming silently skipped.

## Robustness

- **Document browser-session behavior when the optional service is disabled**
  *(confidence: design question; priority: low)*
  The new 503 gate intentionally hides persisted browser-session metadata when
  BROWSER_SERVICE_URL is absent. Decide whether that feature-disabled contract
  should be made explicit in API documentation.

## Evaluation

- **Exercise configured optional-service paths**
  *(confidence: verified configuration gap; priority: medium)*
  When a browser service, agent beta endpoint, support service, or local OCR
  adapter is intentionally configured, run their corresponding create/list or
  parse probes and capture results separately from the base-stack smoke matrix.
