# Forward queue after local API updates (2026-08-11)

Candidate work surfaced during this session. This is a menu, not a roadmap. Verify each item before acting.

## Hardening

- **Add a clean-context Docker regression test for generated NAPI bindings** (priority: high, confidence: verified gap)
  The explicit Docker rebuild now protects deployment, but a small CI test should prove that stale `native/index.d.ts` cannot hide a newly exported Rust binding. This protects future upstream native additions.

- **Make the local capability matrix route policy-aware** (priority: medium, confidence: verified limitation)
  The matrix now uses fresh runtime evidence, but registered routes that need external services remain inferred. Add an explicit policy/configuration column so a route is never mistaken for locally enabled just because it exists upstream.

## New capability

- **Design a page-aware Docling adapter contract before exposing page markdown** (priority: medium, confidence: verified limitation)
  Define and test the Fire PDF physical-page response with stable page numbers and OCR metadata. Do not add a helper flag until API-to-adapter integration proves that contract.

## Process and docs

- **Consider an explicit local API rebuild check after future upstream syncs** (priority: medium, confidence: observed)
  The source tree was current while the running image was stale. Add a post-sync check that records source SHA, image creation time, and health so users know whether local runtime has actually adopted the merge.
