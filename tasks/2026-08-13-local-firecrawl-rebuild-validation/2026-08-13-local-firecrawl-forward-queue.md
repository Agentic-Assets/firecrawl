# Forward queue after local Firecrawl rebuild (2026-08-13)

Candidate work surfaced during the rebuild. This is a menu, not a roadmap;
verify each item before starting it.

## Correctness

- **Add a fixture-based document-structure gate for AGENTIC-2256**
  (priority: high, confidence: verified gap).
  Obtain an owner-approved, non-sensitive DOCX/PDF pair with expected heading,
  table, and page-boundary assertions. Use it to decide whether any parser
  adjustment is warranted, rather than heuristically converting uppercase text
  to headings.

- **Make the CRE Python suite hermetic** (priority: high, confidence: verified
  gap).
  Replace the absent ignored Cushman repair-artifact dependency with an
  owner-maintained integrity check and a tracked synthetic contract fixture.
  Keep live source artifacts and data access out of the offline test suite.

## Hardening

- **Add a negative terminal-crawl integration fixture** (priority: medium,
  confidence: verified helper behavior with unit-only failure coverage).
  A deterministic local API fixture for failed/cancelled crawls would prove the
  helper's nonzero exit and saved artifact over the real HTTP transport.

- **Promote the public long-PDF page-cap probe into scheduled local validation**
  (priority: medium, confidence: verified regression risk).
  Keep the unit test as the fast gate and periodically run the local API probe
  to catch image/dependency drift in the container path.

## Simplification

- **Reassess legacy agent workflow examples** (priority: low, confidence:
  informed suggestion).
  After local users adopt the SDK plus one helper model, identify any older
  wrappers that duplicate generic request/response handling and deprecate them
  only after their documented use cases are migrated.

## Evaluation and operations

- **Add an SDK-local-URL example test outside the CRE collector** (priority:
  medium, confidence: documented upstream capability).
  Verify one upstream SDK configured for `http://localhost:3002` against the
  rebuilt stack so application guidance remains concrete without changing
  collector code.

- **Provision optional capabilities only through their own change packets**
  (priority: medium, confidence: verified configuration gate).
  Browser, agent, AI extraction, and OCR should each gain a readiness probe,
  resource proof, rollback path, and a separate approval before being enabled.
