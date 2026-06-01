# Cursor PDF OCR Handoff Implementation Report

Date: 2026-05-24

## Best Path Forward

Keep Docling Serve as the local OCR/layout engine behind the FirePDF-compatible adapter. The right next step is not a second OCR engine by default; it is stable metadata, reproducible settings fingerprints, cache-safe OCR runs, and benchmark artifacts that let Cursor PDF decide accept/reject/manual-review before any OCR output is promoted.

This matches Docling's current strengths: Markdown export for agent-readable text, JSON export/provenance for QA, page-break placeholders for page chunks, and table/layout options that can be profiled per document family.

Useful official references:

- Docling document model and provenance: https://docling-project.github.io/docling/concepts/docling_document/
- Docling document export reference: https://docling-project.github.io/docling/reference/docling_document/
- Docling Serve usage/options: https://github.com/docling-project/docling-serve/blob/main/docs/usage.md
- Docling table export example: https://docling-project.github.io/docling/examples/export_tables/

## Implemented

- Stable `data.metadata.pdfOcr` on successful OCR responses:
  - adapter version and script fingerprint
  - active profile and profile registry hash
  - stable settings fingerprint over profile/env/options/adapter/Docling image details
  - resolved Docling options
  - low-quality gate settings
  - text quality metrics
  - page count and nonempty page count
  - compact per-page summaries
  - repeated text/page metrics
  - boilerplate score and matched boilerplate families
  - page-boundary source
  - Docling image identifier
  - compact Docling JSON table/picture/provenance summary
- OCR-mode FirePDF cache is bypassed so local canaries do not reuse stale profile/env output.
- Async FirePDF result schema now preserves OCR metadata too.
- `local_firepdf_ocr.sh doctor --smoke-pdf <path>` proves Firecrawl API -> local adapter -> Docling.
- `pdf_ocr_benchmark.py` now surfaces OCR fingerprint, boundary source, boilerplate score, Docling table/picture signals, OCR warnings, and accept/reject/manual-review guidance.
- Skills and durable docs now describe the metadata, cache behavior, doctor smoke, and benchmark outputs.

## Validation

- Adapter unit tests: `python3 scripts/firecrawl-ops/tests/test_local_firepdf_ocr_service.py`
- Python syntax: `python3 -m py_compile scripts/firecrawl-ops/local_firepdf_ocr_service.py scripts/firecrawl-ops/pdf_ocr_benchmark.py`
- Shell syntax: `bash -n scripts/firecrawl-ops/local_firepdf_ocr.sh`
- API tests: `pnpm test -- src/scraper/scrapeURL/engines/pdf/__tests__/firePDF.test.ts src/scraper/scrapeURL/engines/pdf/__tests__/firePDFAsync.test.ts src/lib/__tests__/error-serde-pdf-ocr.test.ts --runInBand`
- API build: `pnpm build`
- Live stack: `scripts/firecrawl-ops/firecrawl_healthcheck.sh`
- Live OCR smoke: `scripts/firecrawl-ops/local_firepdf_ocr.sh smoke apps/test-site/public/example.pdf`

The live smoke returned `data.metadata.pdfOcr.profile=research-page-aware`, a settings fingerprint, `low_quality=false`, page-count metadata, and `cache.safe_for_reuse=false`.

## Benchmark Artifacts

Saved under:

- `tasks/tmp/cursor-pdf-ocr-firecrawl-benchmark-20260524`
- `tasks/tmp/cursor-pdf-ocr-firecrawl-rfs001-20260524`

The tested REE/RFS samples did not reproduce the earlier publisher-boilerplate loop. They returned page-aware OCR with `low_quality=false`, `boilerplate_score=0`, and `docling_markdown_page_break` boundaries. The benchmark still marks them `manual_review` because only OCR mode was tested in this focused canary and `abstract_not_detected_early` remains a cautious QA warning.

## Remaining Cursor-Side Boundary

Firecrawl now supplies OCR output, metadata, stable fingerprints, cache-safe semantics, and benchmark reports. Cursor PDF should still own promotion decisions, package provenance, OCR-needed manifests, and preventing OCR text from replacing `current/` unless its own deterministic gates pass.
