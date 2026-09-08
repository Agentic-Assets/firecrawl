# Forward queue after MR-01 source freshness (2026-09-08)

Candidate work surfaced during MR-01. This is a menu, not a roadmap.

## Evaluation

- **Run two authorized source canaries** (priority: required; confidence:
  verified gate). Reconcile native source identifiers, unchanged inventory,
  queue counts, receipt digests, and last-good preservation before runtime
  activation.
- **Add disposable-database contract rehearsal** (priority: high; confidence:
  verified gap). Exercise the Firecrawl receipt through GetCREdata publication
  and the owner-only SQL gate with allowed and denied roles.

## Hardening

- **Make queue failure class explicit in storage** (priority: medium;
  confidence: verified limitation). A later reviewed migration could store a
  bounded failure enum so reasons beyond the known repeated-absence case do not
  remain unclassified.
- **Repair baseline time-dependent tests** (priority: medium; confidence:
  verified gap). Replace expired July/August wall-clock fixtures with injected
  clocks, restore or generate the protected Cushman artifact fixture, and fix
  the Bash empty-array healthcheck regression.

## Simplification

- **Generate the 51-source vocabulary from one reviewed contract artifact**
  (priority: low; confidence: hypothesis). Consider this only after MR-00
  finalizes object ownership; keep the current cross-repository equality test
  until then.

## Robustness

- **Document corrupt canonical-receipt recovery** (priority: low; confidence:
  verified behavior). The publisher preserves an invalid canonical receipt and
  emits a degraded status. Add an operator procedure for quarantine and
  evidence-preserving replacement after the first live exercise.
