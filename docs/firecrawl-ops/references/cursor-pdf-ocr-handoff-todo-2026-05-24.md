# Cursor PDF OCR Handoff Todo

Date: 2026-05-24  
Audience: coding agent working in `/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl`  
Upstream consumer: Cursor PDF extraction skill in `/Users/caymanseagraves/Documents/GitHub/agentic-assets/agentic-assets-orbis/.agents/skills/cursor-pdf-extraction`

## Goal

Make the local Firecrawl/Docling OCR path robust enough for a later, separate Cursor PDF OCR recovery lane. Do **not** assume OCR should be enabled for the active 16k native-PDF production run. That run should continue with OCR off; this work is for the quarantined `OCR_needed` subset.

The Cursor PDF side will add deterministic OCR quality gates before promoting OCR output into `current/` or spending Composer calls. Firecrawl should make those gates easier, more reproducible, and better instrumented.

## Current Firecrawl OCR Capabilities

- Local `/v2/parse` supports PDF modes `fast`, `auto`, and `ocr`.
- `ocr` can route to the local FirePDF-compatible adapter at `127.0.0.1:31337`.
- The adapter delegates to Docling Serve at `127.0.0.1:5001`.
- Named profiles exist in `scripts/firecrawl-ops/pdf_ocr_profiles.json`, including:
  - `research-page-aware`
  - `tables-accurate`
  - `tables-fast`
  - `scanned-english`
  - `qa-debug`
- `research-page-aware` can emit `FIRECRAWLPAGEBREAK`.
- Recent hardening added:
  - OCR concurrency backpressure: HTTP 429 / `SCRAPE_PDF_OCR_BACKPRESSURE`
  - timeout mapping: HTTP 504 / `SCRAPE_PDF_OCR_TIMEOUT`
  - low-quality rejection: HTTP 422 / `SCRAPE_PDF_LOW_QUALITY`
  - possible OCR quality metadata at `data.metadata.pdfOcr`
  - benchmark tooling: `scripts/firecrawl-ops/pdf_ocr_benchmark.py`

## Important Known Failure Pattern

Previous OCR canaries showed publisher boilerplate being promoted as paper text:

- one nonempty page with Wiley/OUP download/license text
- many empty pages
- enough raw characters to pass naive text-density checks
- downstream package could appear mechanically valid

Firecrawl-side low-quality rejection is now supposed to catch this, but the Cursor PDF side needs stable metadata to verify and audit it.

## Priority Todo For Firecrawl Agent

### P0 — Required Before Cursor PDF OCR Canary

1. **Verify the latest low-quality OCR rejection on known bad journal PDFs.**
   - Use REE/Wiley and RFS/OUP samples that previously produced publisher boilerplate loops.
   - Expected behavior: HTTP 422 / `SCRAPE_PDF_LOW_QUALITY`, or a successful response with clear `pdfOcr` metadata showing high-risk quality metrics.
   - Save benchmark artifacts and a short report.

2. **Make `data.metadata.pdfOcr` complete and stable for every successful OCR response.**
   Include:
   - adapter version or script fingerprint
   - active profile name
   - resolved Docling options
   - low-quality gate settings
   - text quality metrics
   - page count / nonempty page count
   - repeated-line or repeated-page metrics
   - boilerplate score / matched boilerplate families
   - whether page boundaries came from `FIRECRAWLPAGEBREAK`, Docling JSON provenance, or estimation
   - Docling image identifier/digest if available

3. **Add a settings/profile fingerprint.**
   - Stable hash over profile name, profile JSON, env overrides, Docling options, adapter version, and relevant Firecrawl PDF parser settings.
   - This lets Cursor PDF avoid reusing stale OCR attempts after settings change.

4. **Confirm cache behavior is profile/settings safe.**
   - Either make FirePDF/OCR cache keys include the settings fingerprint, or document/implement a way to bypass cache for OCR canary and benchmark runs.
   - The Cursor PDF canary should not accidentally read stale OCR generated under a different profile.

5. **Extend `local_firepdf_ocr.sh doctor` with an optional actual OCR smoke.**
   - Example: `local_firepdf_ocr.sh doctor --smoke-pdf /path/to/sample.pdf`
   - It should prove the full route: Firecrawl API -> local adapter -> Docling -> response metadata.

### P1 — Strongly Recommended Before Medium OCR Queue

6. **Expose first-class page artifacts if feasible.**
   - Ideal metadata shape: `pages:[{page,text_char_count,quality_flags,boundary_source}]`.
   - Do not embed huge full-page text by default; a compact summary is enough for Cursor quality gates.

7. **Expand boilerplate pattern coverage.**
   Cover at least:
   - Wiley / onlinelibrary
   - OUP / academic.oup
   - Elsevier / ScienceDirect
   - Springer
   - Taylor & Francis
   - JSTOR
   - SSRN cover/download wrappers
   - repeated DOI/header/footer loops

8. **Add retry guidance to OCR errors.**
   - 429 should include retryable true and suggested delay.
   - 504 should include timeout context and whether lowering `maxPages` is recommended.
   - 422 should include enough metrics for Cursor to classify as hard rejection vs manual review.

9. **Improve benchmark report for Cursor PDF use.**
   `pdf_ocr_benchmark.py` should summarize:
   - best mode/profile per PDF
   - page-boundary source
   - low-quality result
   - boilerplate score
   - nonempty page ratio
   - table/figure signals
   - recommendation: accept / reject / manual review

### P2 — Useful Later

10. **Evaluate whether Docling JSON tables can be exposed compactly.**
    - Cursor PDF currently preserves native PyMuPDF visual/table artifacts.
    - Firecrawl should not replace that yet, but table metadata could help OCR-only scanned papers.

11. **Profile-specific reproducibility report.**
    - Run the same small OCR sample 3 times under `research-page-aware`.
    - Confirm markdown/page metrics are stable enough for resumable batch processing.

12. **Document resource envelope.**
    - Recommended `LOCAL_FIREPDF_MAX_CONCURRENT_OCR`.
    - Expected seconds/page for scanned vs born-digital PDFs.
    - Memory/CPU pressure guidance.

## Suggested Validation Commands

From Firecrawl repo:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor
scripts/firecrawl-ops/local_firepdf_ocr.sh settings
scripts/firecrawl-ops/local_firepdf_ocr.sh health
```

Benchmark a small sample:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py /path/to/bad-journal.pdf /path/to/scanned-good.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate,scanned-english \
  --max-pages 10 \
  --out-dir tasks/tmp/cursor-pdf-ocr-firecrawl-benchmark-20260524 \
  --strict
```

Direct parse smoke:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse /path/to/bad-journal.pdf \
  --formats markdown,html,images \
  --pdf-mode ocr \
  --max-pages 10 \
  --out-dir tasks/tmp/cursor-pdf-ocr-smoke-20260524 \
  --save-fields tasks/tmp/cursor-pdf-ocr-smoke-20260524/fields \
  --timeout 900 \
  --quiet --print-paths
```

## Acceptance Criteria For Cursor PDF Integration

Firecrawl side is ready for Cursor PDF parse-only OCR canary when:

- low-quality publisher boilerplate samples fail with 422 or unambiguously quality-failed metadata
- successful OCR responses include stable `data.metadata.pdfOcr`
- profile/settings fingerprint is present and reproducible
- page-boundary source is visible
- benchmark artifacts show which profile/mode should be used
- doctor/smoke command proves the local route end-to-end

## Boundary With Cursor PDF Repo

Firecrawl should provide OCR output, metadata, and stable failure semantics. The Cursor PDF skill will own:

- deciding whether OCR output is accepted for a paper package
- preventing bad OCR from replacing `current/`
- creating `reports/ocr-needed-manifest.jsonl`
- running parse-only OCR before paid Composer
- preserving OCR provenance in document packages and exports

Do not change Cursor PDF production run behavior from the Firecrawl repo. The active 16k run should remain OCR-off.
