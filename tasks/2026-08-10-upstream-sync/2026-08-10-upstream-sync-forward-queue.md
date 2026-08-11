# Forward queue after upstream sync (2026-08-10)

Candidate work surfaced during the sync. This is a menu, not a roadmap. Verify each item before acting.

## Hardening

- **Add a containerized FirePDF-to-Docling contract test** (priority: high, confidence: verified gap)
  Exercise API to adapter to Docling with a pinned fixture, verify typed 429/422/504 mapping, metadata, forced-OCR cache bypass, and the explicit page-markdown behavior. This would close the current deliberate `.env` mutation avoidance.

- **Gate the native API build in the declared container runtime** (priority: high, confidence: verified gap)
  The host lacks FoundationDB headers and has an unusable local native converter export. A reproducible container build-and-snips gate would distinguish machine setup from a Firecrawl-rs packaging regression.

- **Make the CRE fixture contract self-describing** (priority: high, confidence: verified gap)
  The collector suite depends on a reviewed local artifact that is absent. Add a source-governed fixture manifest and a precise skip or setup diagnostic, without fabricating or downloading a production-derived artifact in tests.

## Robustness

- **Investigate the OCR lifecycle wrapper under noninteractive execution** (priority: medium, confidence: observed)
  `local_firepdf_ocr.sh start-adapter` remained blocked in this execution environment after the profile helper, while equivalent explicit Docker build/run completed successfully. Reproduce under an ordinary terminal and make the wrapper report progress or fail deterministically.

- **Make the OM-facts Postgres contract runner noninteractive-safe** (priority: medium, confidence: observed)
  Its Docker pipeline hung before issuing the contract queries. Preserve the no-live-write guarantee and replace the fragile pipeline with a bounded command that emits a ready/failure diagnostic.

## Process and test maintenance

- **Skip cloud Python E2E collection when credentials are absent** (priority: medium, confidence: verified gap)
  The suite raises at collection time without `API_KEY` rather than marking cloud tests skipped. A clear skip keeps local coverage signal readable.

- **Extend the upstream sync preflight script** (priority: medium, confidence: verified gap)
  Add untracked-file checks, protected fork-surface manifest checks, merge-tree conflict preview, pnpm-major validation, FirePDF cache assertions, and post-merge verification reporting. The existing helper is too narrow for a 236-commit upstream merge.
