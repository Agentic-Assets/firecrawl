# Local PDF OCR Research Agent Plan

Date: 2026-05-23

## Decision

Keep Docling Serve behind the local FirePDF-compatible adapter as the default OCR/layout engine for research-paper PDFs.

This remains the best fit because it:

- Reuses Firecrawl's existing Fire PDF boundary instead of editing upstream-owned PDF engine code.
- Runs locally through OrbStack without Firecrawl cloud credits.
- Produces Markdown, HTML, and Docling JSON from one conversion.
- Supports OCR, PDF backends, table modes, image export modes, page-break markers, formulas, charts, and picture enrichment.
- Preserves Firecrawl's fast local text path for born-digital PDFs where OCR is slower or less complete.

The next improvement should not be "replace Docling." The next improvement should be a better research-agent layer around Docling:

1. Named OCR profiles.
2. Page boundary preservation.
3. Raw Docling JSON capture for debugging and table/layout recovery.
4. Profile-aware benchmarks.
5. OCR quality reports that tell agents which parser mode/profile to trust.

## Current Local Shape

The current architecture is already the right boundary:

```text
Agent / CLI / API caller
        |
        v
Local Firecrawl API :3002
        |
        | /v2/parse with PDF parser mode auto or ocr
        v
Firecrawl PDF engine
        |
        | FIRE_PDF_BASE_URL=http://host.docker.internal:31337
        v
Local FirePDF-compatible adapter :31337
        |
        v
Docling Serve :5001
```

Keep all new work fork-owned unless a clear API need appears:

- `scripts/firecrawl-ops/`
- `docs/firecrawl-ops/references/`
- `.agents/skills/`
- `LOCAL_DEVELOPMENT_GUIDE.md`
- `AGENTS.md`

Avoid touching these upstream-owned files unless phase-two per-request profile support is needed:

- `apps/api/src/controllers/v2/types.ts`
- `apps/api/src/scraper/scrapeURL/engines/pdf/firePDF.ts`
- `apps/api/src/scraper/scrapeURL/engines/pdf/fire-pdf/*`
- Firecrawl PDF cache code

## Most Useful Next Feature

The highest-value next feature is a profile-aware benchmark and artifact system, not a new OCR engine.

Agents working on academic PDFs need to answer practical questions:

- Is this PDF born-digital, scanned, image-only, slide-like, table-heavy, or figure-heavy?
- Should this run use Firecrawl `fast`, `auto`, or `ocr`?
- If using local OCR, which Docling profile is best?
- Did the output preserve page boundaries?
- Are tables and figures visible enough for downstream extraction?
- Where is the raw Docling JSON if Markdown flattened something important?

Build the tooling around those questions.

## Phase 1: Named OCR Profiles

Add a small JSON profile registry:

```text
scripts/firecrawl-ops/pdf_ocr_profiles.json
```

The profile file should be boring, portable JSON so shell, Python, and other agents can read it.

Recommended first profiles:

| Profile | Purpose | Key options |
| --- | --- | --- |
| `default` | Conservative general local OCR | Current Docling defaults: `to_formats=md,json,html`, `pdf_backend=docling_parse`, `table_mode=accurate`, OCR on |
| `research-page-aware` | Academic papers where agents need page chunks | Same as default plus `md_page_break_placeholder=FIRECRAWLPAGEBREAK` |
| `tables-accurate` | Finance/academic papers with important tables | Accurate table mode, table cell matching on, JSON capture recommended |
| `tables-fast` | Quicker table/layout pass for long papers | `table_mode=fast`, page breaks on, lower max pages during benchmark |
| `scanned-english` | Scanned/image-only English PDFs | OCR preset/language explicit, force OCR on, page breaks on |
| `qa-debug` | Investigation mode | Page breaks on, raw Docling JSON capture on, optional image placeholders/references |
| `figure-enrichment-lab` | Experiment for charts/figures | Chart extraction and picture description enabled only for benchmarks |

Merge order:

1. Adapter built-in safe defaults.
2. Named profile from `LOCAL_FIREPDF_PROFILE`.
3. Explicit env overrides such as `LOCAL_FIREPDF_DOCLING_TABLE_MODE`.
4. Direct adapter `docling_options` overrides for controlled tests.

Do not expose public `/v2/parse` profile options in phase 1. Firecrawl API calls should use the adapter's process-level profile. Direct adapter benchmark calls can use per-request `docling_options`.

## Phase 2: Page Boundary Preservation

Use Docling's `md_page_break_placeholder` option. Prefer a visible alphanumeric marker that survives Firecrawl's markdown/html normalization:

```text
FIRECRAWLPAGEBREAK
```

Why this matters:

- Firecrawl's `/v2/parse` currently returns one Markdown string.
- OCR Markdown can otherwise be hard to split into reliable pages.
- Page-aware artifacts make academic workflows much stronger: page citations, figure/table inspection, partial reruns, and side-by-side parser comparisons.

Implementation approach:

- Keep the default profile clean if downstream consumers dislike page markers.
- Enable page markers in `research-page-aware`, `tables-*`, `scanned-english`, and `qa-debug`.
- Extend `pdf_ocr_benchmark.py` to count page markers and save page artifacts.

Recommended benchmark output additions:

```text
fields/markdown.md
fields/html.html
fields/metadata.json
fields/pages.jsonl
qa.json
qa.md
```

Each `pages.jsonl` record should include:

- `page_index`
- `markdown`
- `char_count`
- `word_count`
- simple table/figure marker counts
- warnings for very low text, repeated text, or empty pages

## Phase 3: Raw Docling JSON Capture

The adapter already has a raw debug save path through `LOCAL_FIREPDF_OUTPUT_DIR`, but it should be made easier and safer to use.

Make raw JSON capture:

- Explicit.
- Off by default.
- Easy to enable from the wrapper.
- Saved outside tracked source paths by default.
- Mentioned clearly as full-document sensitive data.

Recommended defaults:

```text
LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=false
LOCAL_FIREPDF_OUTPUT_DIR=tasks/tmp/firecrawl-docling-debug
```

Wrapper improvements:

- Add `local_firepdf_ocr.sh profiles` to list profiles.
- Add `local_firepdf_ocr.sh profile-env <profile>` to print export commands.
- Add `local_firepdf_ocr.sh start --profile research-page-aware`.
- Add `local_firepdf_ocr.sh restart-adapter --profile qa-debug --capture-json`.
- Mount the chosen output directory into the adapter container when capture is enabled.

Saved raw files should include enough context to connect them back to benchmark cases:

```text
<timestamp>-<scrape_id>-<profile>-docling.json
<timestamp>-<scrape_id>-<profile>-settings.json
```

## Phase 4: Profile-Aware Benchmarking

Extend `pdf_ocr_benchmark.py` from a mode matrix to a mode plus profile matrix.

Current:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./paper.pdf \
  --modes fast,auto,ocr --max-pages 40 --strict
```

Target:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./paper.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate,scanned-english \
  --max-pages 40 \
  --out-dir tasks/tmp/firecrawl-pdf-ocr-benchmark \
  --strict
```

Benchmark behavior:

- Run `fast` without Docling profile work where possible.
- Run `auto`/`ocr` with profile-specific adapter settings.
- Prefer direct adapter `docling_options` for fast profile experiments.
- Run end-to-end Firecrawl parse for the winning profile/mode if needed.
- Save profile settings with every case.
- Keep `maxPages` explicit; never surprise-run an 800-page OCR benchmark.

Recommendation logic should consider:

- Success/failure.
- Runtime and seconds per page.
- Markdown length and words per page.
- Page-break count vs expected pages.
- Empty/near-empty page count.
- Table marker density.
- Figure/image/caption signals.
- Raw Docling JSON availability.
- Whether OCR produced less content than `fast` on a born-digital PDF.

## Phase 5: OCR QA Reports

Add per-case QA artifacts:

```text
qa.json
qa.md
```

Suggested checks:

- `low_text_pages`: pages with very low word count.
- `missing_page_breaks`: processed pages exceed marker-derived pages.
- `replacement_chars`: count of Unicode replacement characters.
- `repeated_line_ratio`: likely OCR duplication or header/footer loops.
- `table_signal_count`: Markdown table pipes, HTML tables, or Docling JSON table blocks.
- `figure_signal_count`: figure/caption/image markers.
- `abstract_signal`: whether "abstract" appears early.
- `references_signal`: whether references/bibliography appears near the end.
- `elapsed_per_page`.
- `warnings`: concise human-readable warnings.

Agents should treat this as routing guidance, not proof. The report can say "try `fast`," "try `research-page-aware`," "rerun first 10 pages with `qa-debug`," or "inspect raw JSON for tables."

## Optional Academic Enrichment

Docling should remain the main local OCR/layout default. Add other engines only when they improve a real paper corpus.

Promising additions:

1. GROBID companion service.
   - Best for scholarly metadata, citations, references, affiliations, and TEI.
   - Not a scanned-PDF OCR replacement.
   - Useful after Docling/Firecrawl parse for academic research packages.

2. PaddleOCR PP-StructureV3 or PaddleOCR-VL benchmark profile.
   - Strong candidate for charts, formulas, table-heavy layouts, and hard scans.
   - Heavier than Docling, especially on this Mac without NVIDIA GPU.
   - Add only as an optional benchmark engine behind the adapter abstraction.

3. AI2 olmOCR high-accuracy profile.
   - Strong reputable VLM OCR path for difficult academic and technical PDFs.
   - Needs a 7B-class local or OpenAI-compatible inference server for serious local use.
   - Keep as a future high-power profile, not the default Mac CPU path.

Fallbacks:

- Tesseract/OCRmyPDF: excellent for creating searchable text layers; weaker for layout/table-to-Markdown.
- Apache Tika: broad parser and metadata fallback; not a high-fidelity research-paper OCR engine.
- Marker/Surya: technically interesting, but licensing/model terms make them less comfortable as default fork infrastructure.

## Cache And Concurrency Risks

Phase 1 profile support is process-level. That is acceptable for local agent workflows but means concurrent OCR parses use the same adapter profile.

Avoid passing profile names through Firecrawl `/v2/parse` until there is a real need. If phase 2 requires per-request profiles through the Firecrawl API, then:

- Add internal-only parser fields such as `__firePdfProfile` or `__firePdfDoclingOptions`.
- Pass those fields to FirePDF.
- Include a profile/options hash in FirePDF cache variants.
- Add focused API tests proving profile A cannot reuse cached profile B output.

Without cache changes, per-request profile output can contaminate cached OCR results.

## Recommended Implementation Order

1. Add `pdf_ocr_profiles.json`.
2. Teach `local_firepdf_ocr_service.py` to load and merge named profiles.
3. Teach `local_firepdf_ocr.sh` to list/apply profiles and enable raw JSON capture cleanly.
4. Add page artifact generation to `pdf_ocr_benchmark.py`.
5. Add `--profiles` support and QA reports to `pdf_ocr_benchmark.py`.
6. Update `firecrawl-ops` and `firecrawl-local-api` skills with the new profile/benchmark workflow.
7. Run `scripts/firecrawl-ops/sync_agent_skills.sh`.
8. Benchmark a real academic-paper set:
   - born-digital research paper
   - scanned/image-only paper
   - table-heavy finance/CRE paper
   - figure-heavy paper
   - long 40+ page report
9. Promote only the profiles that win on real outputs.

## Validation Plan

Static checks:

```bash
python3 -m py_compile \
  scripts/firecrawl-ops/local_firepdf_ocr_service.py \
  scripts/firecrawl-ops/pdf_ocr_benchmark.py

bash -n scripts/firecrawl-ops/local_firepdf_ocr.sh
```

Runtime checks:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh smoke apps/test-site/public/example.pdf
```

Benchmark checks:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py \
  apps/test-site/public/example.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,qa-debug \
  --max-pages 3 \
  --strict
```

Longer research check:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./paper.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate,scanned-english \
  --max-pages 40 \
  --out-dir tasks/tmp/firecrawl-pdf-ocr-benchmark-research \
  --strict
```

## Sources Checked

- Docling pipeline options: https://docling-project.github.io/docling/reference/pipeline_options/
- Docling CLI reference: https://docling-project.github.io/docling/reference/cli/
- Docling Serve repository/API examples: https://github.com/docling-project/docling-serve
- PaddleOCR-VL documentation: https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html
- AI2 olmOCR repository: https://github.com/allenai/olmocr
- AI2 olmOCR overview: https://olmocr.allenai.org/
- Existing local plan: `docs/firecrawl-ops/references/local-pdf-ocr-plan.md`
- Existing adapter: `scripts/firecrawl-ops/local_firepdf_ocr_service.py`
- Existing benchmark helper: `scripts/firecrawl-ops/pdf_ocr_benchmark.py`

## Final Recommendation

Do this next:

```text
Docling profiles + page breaks + raw JSON capture + profile-aware benchmark + OCR QA reports
```

Do not add a second OCR engine yet. First make the current Docling path measurable, debuggable, and page-aware for research-paper agents. After that, benchmark GROBID as a companion and PaddleOCR/olmOCR as optional challengers against the same saved output format.
