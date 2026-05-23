# Local PDF OCR Plan

Date: 2026-05-23

## Recommendation

Use a local FirePDF-compatible adapter backed by Docling Serve as the default OCR/layout path for this fork.

This is the strongest first path because it keeps upstream Firecrawl code untouched, uses Firecrawl's existing PDF OCR integration boundary, runs locally without Firecrawl cloud credits, and gives agents a more robust path for scanned, table-heavy, figure-heavy, and multi-column PDFs than the default local parser.

The adapter should live in this fork's ops layer, not in upstream-owned API code:

- `scripts/firecrawl-ops/` for runnable service wrappers and helper scripts
- `docs/firecrawl-ops/references/` for durable docs
- `.agents/skills/` for agent-facing instructions

Do not replace Firecrawl's built-in Rust PDF extraction. Keep it as the fast default for normal text PDFs. Add local OCR as an optional fallback path for hard PDFs.

## Implementation Status

Implemented and smoke-tested on 2026-05-23:

- `scripts/firecrawl-ops/local_firepdf_ocr_service.py` implements the FirePDF-compatible `POST /ocr` adapter and calls Docling Serve's current `/v1/convert/source` `sources` contract.
- `scripts/firecrawl-ops/local_firepdf_ocr.sh` starts Docling Serve and the adapter in OrbStack/Docker. The default Docling Serve CPU image is pinned by digest for repeatability; override `LOCAL_FIREPDF_DOCLING_IMAGE` to intentionally test newer releases.
- `scripts/firecrawl-ops/pdf_ocr_benchmark.py` runs a saved `fast` / `auto` / `ocr` comparison matrix.
- `set_model_profile.sh` preserves existing local OCR routing values when changing LLM profiles.
- Verified: direct adapter `/ocr`, local Firecrawl `/v2/parse` with `mode:"ocr"`, local API healthcheck, CLI wrapper parse smoke, and a two-PDF benchmark matrix.

Useful dynamic Docling knobs before `local_firepdf_ocr.sh start-adapter` or `start`:

- `LOCAL_FIREPDF_DOCLING_OCR_PRESET=auto|easyocr|tesseract`
- `LOCAL_FIREPDF_DOCLING_OCR_LANG=en[,de,...]`
- `LOCAL_FIREPDF_DOCLING_PDF_BACKEND=docling_parse|pypdfium2|dlparse_v4`
- `LOCAL_FIREPDF_DOCLING_TABLE_MODE=accurate|fast`
- `LOCAL_FIREPDF_DOCLING_TO_FORMATS=md,json,html`
- optional enrichment flags such as `LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION=true` or `LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION=true`

## Local Firecrawl Fit

This plan is designed for the current self-hosted Firecrawl fork on this Mac:

- OrbStack provides the Docker runtime.
- Firecrawl API is expected at `http://localhost:3002`.
- The repo-root `.env` is the file Docker Compose reads; it is gitignored and must not be committed.
- Fork-owned operational tools live under `scripts/firecrawl-ops/`.
- Fork-owned durable docs live under `docs/firecrawl-ops/references/`.
- Agent-facing skills are canonical in `.agents/skills/` and can be copied/symlinked to user-level agent folders with `scripts/firecrawl-ops/sync_agent_skills.sh`.

The implementation should preserve the fork's upstream-sync posture:

- Do not add Docling, PaddleOCR, or OCR services directly to upstream-owned Firecrawl API code.
- Do not make the main Firecrawl API depend on local OCR being available.
- Do not require cloud Firecrawl credentials.
- Keep local OCR opt-in through `.env` and helper scripts.
- Keep text-PDF parsing fast when OCR is not needed.

Expected local behavior after implementation:

- `--pdf-mode fast`: no OCR; good for cheap born-digital text PDFs.
- `--pdf-mode auto`: use Firecrawl's local Rust text extraction first; fall back to local OCR only when Firecrawl's normal PDF path needs it.
- `--pdf-mode ocr`: force the OCR path so scanned/image-only PDFs can use the local adapter.

## Why This Shape

Firecrawl already has a clean FirePDF client boundary. When `FIRE_PDF_ENABLE=true` and `FIRE_PDF_BASE_URL` is set, the API can call a service at:

```text
POST ${FIRE_PDF_BASE_URL}/ocr
```

That means we can add a local service that speaks the FirePDF sync contract and point the API container at it through OrbStack's `host.docker.internal`.

This avoids editing upstream-owned files such as:

- `apps/api/src/scraper/scrapeURL/engines/pdf/index.ts`
- `apps/api/src/scraper/scrapeURL/engines/pdf/firePDF.ts`
- `apps/api/src/config.ts`

Keeping the OCR bridge fork-owned makes future `firecrawl/firecrawl:main` syncs much easier.

## Target Architecture

```text
Agent / CLI / API caller
        |
        v
Local Firecrawl API :3002
        |
        | PDF parser mode auto or ocr
        v
Firecrawl PDF engine
        |
        | FIRE_PDF_BASE_URL=http://host.docker.internal:31337
        v
Local FirePDF-compatible adapter :31337
        |
        | default engine
        v
Docling Serve :5001
        |
        v
Markdown / structured document output
```

The adapter should expose Firecrawl's expected sync endpoint and hide the Docling-specific API shape from Firecrawl.

## FirePDF-Compatible Contract

The local adapter should accept:

```http
POST /ocr
Content-Type: application/json
```

Request body fields Firecrawl may send:

```json
{
  "pdf": "<base64-pdf>",
  "scrape_id": "scrape-id",
  "max_pages": 25,
  "mode": "auto",
  "team_id": "team-id",
  "crawl_id": "crawl-id",
  "url": "source-url",
  "pdf_sha256": "sha256",
  "source": "firecrawl",
  "zdr": false,
  "timeout": 120000,
  "created_at": 1779566400000
}
```

Required response:

```json
{
  "markdown": "# Extracted document...",
  "failed_pages": [],
  "pages_processed": 25
}
```

The first implementation can support only the sync `/ocr` endpoint. Firecrawl also has an async FirePDF path (`/jobs`, `/jobs/:id`, `/jobs/:id/result`), but that is not needed for a first local setup.

## Chosen Engine: Docling Serve

Docling Serve should be the default backend for the local adapter.

Reasons:

- Local execution for sensitive or private PDFs
- MIT-licensed codebase
- Official CLI, Python API, and API service
- Official container images with `linux/arm64` support for Apple Silicon and OrbStack
- Advanced PDF understanding: layout, reading order, tables, OCR, formulas, images, Markdown/HTML/JSON exports
- Stronger fit for agent workflows than plain OCR text

Primary references:

- Docling: https://github.com/docling-project/docling
- Docling Serve: https://github.com/docling-project/docling-serve
- Docling full-page OCR example: https://docling-project.github.io/docling/examples/full_page_ocr/

## Validation Notes

The recommendation was checked against current official docs and repo signals on 2026-05-23.

Docling Serve remains the best default because its v1 API directly supports both multipart file conversion and base64 file sources:

- `POST /v1/convert/file` for multipart uploads
- `POST /v1/convert/source` for JSON sources, including base64-encoded files
- output formats including Markdown, JSON, HTML, text, and DocTags
- OCR options including `do_ocr`, `force_ocr`, OCR language, PDF backend, table mode, and image export mode

This maps cleanly to a FirePDF-compatible adapter: receive Firecrawl's `/ocr` JSON body, decode or forward the PDF to Docling Serve, then normalize Docling's Markdown into Firecrawl's expected `{ markdown, failed_pages, pages_processed }` response.

PaddleOCR PP-Structure/PaddleOCR-VL remains the most serious alternate engine, not a better default. Current docs show a rich `/layout-parsing` API for image/PDF input with layout detection, chart recognition, table merging, Markdown output, image output, and structured JSON-like results. It may beat Docling on some hard tables/layouts, but it is a heavier integration surface and should be promoted only after a benchmark on this fork's real PDFs.

AI2 olmOCR is the strongest reputable VLM-based candidate to add to the benchmark list. It comes from the Allen Institute for AI, is Apache-2.0, and targets high-throughput conversion of PDFs and document images into naturally ordered text/Markdown while preserving structures such as tables and equations. It should not be the default first engine for this Mac because the full toolkit is designed around serving a 7B-class vision-language model on local GPUs or an OpenAI-compatible local inference server. Keep it as a high-power optional engine if Docling and PaddleOCR miss important scanned or complex PDFs.

Google-linked Tesseract, Microsoft Table Transformer, Apache Tika, GROBID, LayoutParser, and docTR are all credible, but they do not replace the Docling-first plan:

- Tesseract is mature local OCR, but it is text recognition, not robust PDF-to-Markdown layout extraction.
- Microsoft Table Transformer is strong for table detection/structure, but it needs OCR or PDF text extraction as a separate input and is a table component, not a complete OCR service.
- Apache Tika is a mature ingestion/parser framework and can use Tesseract, but it is not optimized for high-fidelity tables/figures/layout Markdown.
- GROBID is excellent for scholarly born-digital PDFs and citations, but it is not a scanned-PDF OCR engine.
- LayoutParser is a useful academic document-image-analysis toolkit, but it is a component framework rather than a ready FirePDF-compatible service.
- docTR is a strong Apache-2.0 OCR library, but it is closer to text detection/recognition than full document layout-to-Markdown conversion.

OCRmyPDF/Tesseract remains a fallback, not the default. It is mature and useful for making scanned PDFs searchable, but it is not a robust table/layout/figure-to-Markdown engine.

Marker and Surya remain useful experiments, but GPL-3.0 licensing and model/commercial terms make them less comfortable as default fork infrastructure.

MinerU remains a high-power deferred option. It is active and capable, but its local runtime and operational footprint are heavier than Docling Serve for this Mac/OrbStack setup.

## Implementation Steps

## Files To Add Or Update

Keep the write set fork-owned and predictable:

Add:

- `scripts/firecrawl-ops/local_firepdf_ocr_service.py` — FirePDF-compatible local adapter.
- `scripts/firecrawl-ops/local_firepdf_ocr.sh` — start/stop/health/env helper for Docling Serve and the adapter.
- `scripts/firecrawl-ops/pdf_ocr_benchmark.py` — repeatable comparison matrix after the adapter works.

Update:

- `scripts/firecrawl-ops/set_model_profile.sh` — preserve local OCR routing vars unless explicitly reset.
- `.agents/skills/firecrawl-local-api/SKILL.md` — teach agents how to call local PDF OCR through `/v2/parse`, CLI, and helper flags.
- `.agents/skills/firecrawl-ops/SKILL.md` — teach agents how to start/check/stop the local OCR services and explain limits.
- `LOCAL_DEVELOPMENT_GUIDE.md` — document the local OCR setup for humans.
- `docs/firecrawl-ops/references/ops-playbook.md` — add the concise operational runbook.

Avoid:

- Editing upstream-owned Firecrawl PDF engine files unless a real upstream integration bug appears.
- Committing root `.env`.
- Making local OCR mandatory for normal Firecrawl startup.

### 0. Confirm local prerequisites

Before implementing the adapter, verify the local stack and ports:

```bash
docker context show
scripts/firecrawl-ops/firecrawl_healthcheck.sh
lsof -nP -iTCP:3002 -sTCP:LISTEN
lsof -nP -iTCP:31337 -sTCP:LISTEN || true
lsof -nP -iTCP:5001 -sTCP:LISTEN || true
```

Expected:

- Docker context is `orbstack`.
- Firecrawl health check passes.
- Port `3002` is used by local Firecrawl.
- Ports `31337` and `5001` are free unless the OCR adapter or Docling Serve is already running.

Known local tools already present:

- Tesseract
- Poppler (`pdftoppm`, `pdftotext`)

These are useful fallback utilities, but the primary planned backend is Docling Serve.

### 1. Add a local OCR service wrapper

Create a fork-owned adapter script:

```text
scripts/firecrawl-ops/local_firepdf_ocr_service.py
```

Responsibilities:

- Start an HTTP server on `127.0.0.1:31337` by default.
- Implement `GET /health`.
- Implement `POST /ocr`.
- Decode Firecrawl's base64 `pdf` field into a temp file.
- Enforce `max_pages` when possible.
- Call the selected backend engine.
- Normalize backend output into FirePDF-compatible JSON.
- Return FirePDF-compatible JSON.
- Never call Firecrawl cloud.
- Never require a Firecrawl API key.
- Respect Firecrawl's `timeout` / `created_at` fields when possible.
- Write optional debug artifacts only when `LOCAL_FIREPDF_OUTPUT_DIR` is set.

Recommended environment variables:

```bash
LOCAL_FIREPDF_PORT=31337
LOCAL_FIREPDF_ENGINE=docling
LOCAL_FIREPDF_DOCLING_URL=http://127.0.0.1:5001
LOCAL_FIREPDF_DOCLING_FORCE_OCR=true
LOCAL_FIREPDF_DOCLING_TO_FORMATS=md,json,html
LOCAL_FIREPDF_TIMEOUT_SECONDS=240
LOCAL_FIREPDF_OUTPUT_DIR=
LOCAL_FIREPDF_KEEP_TEMP=false
```

Keep the adapter engine-neutral so later tests can compare:

```bash
LOCAL_FIREPDF_ENGINE=docling
LOCAL_FIREPDF_ENGINE=paddle
LOCAL_FIREPDF_ENGINE=ocrmypdf
```

The first committed implementation should support `docling`; the other engine names can be reserved until tested.

Docling response normalization should prefer fields in this order:

1. `document.md_content`
2. `document.text_content`
3. Markdown reconstructed from `document.json_content`
4. empty string only when the backend explicitly returns no content

If Docling returns `status:"partial_success"`, return the available Markdown and map backend errors to `failed_pages` when page-level information is available. If no page-level information is available, keep `failed_pages` as `[]` and include details in adapter logs/debug artifacts.

For `pages_processed`, prefer:

1. Docling page count from response metadata if present
2. Firecrawl's `max_pages` when set and processing succeeded
3. a local page count from `pdfinfo` or a lightweight PDF library
4. `1` as a last resort only for a successful response with unknown count

### 2. Add a start/check helper

Create:

```text
scripts/firecrawl-ops/local_firepdf_ocr.sh
```

Commands:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start-docling
scripts/firecrawl-ops/local_firepdf_ocr.sh start-adapter
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh env
scripts/firecrawl-ops/local_firepdf_ocr.sh stop
```

The helper should prefer OrbStack/Docker for Docling Serve. On this Mac, start with the CPU image because it has `linux/arm64` support and does not require CUDA:

```bash
docker run --rm -p 5001:5001 \
  -e DOCLING_SERVE_ENABLE_UI=1 \
  --name firecrawl-docling-serve \
  quay.io/docling-project/docling-serve-cpu
```

Use a named container only if it makes stop/restart simpler. Avoid modifying the main `docker-compose.yaml` unless there is a strong reason; a separate helper keeps upstream sync cleaner. After the first smoke test, pin the image to the current known-good Docling Serve release tag instead of relying on a floating tag.

If first-run model downloads are slow or brittle, add a persistent local model/cache directory and mount it into the Docling Serve container. The helper should print the cache path so agents know where disk usage is coming from.

The adapter should call Docling Serve using one of these forms:

```bash
curl -X POST http://127.0.0.1:5001/v1/convert/file \
  -F 'files=@./report.pdf;type=application/pdf' \
  -F 'to_formats=md' \
  -F 'to_formats=json' \
  -F 'do_ocr=true' \
  -F 'force_ocr=true'
```

or:

```json
{
  "options": {
    "to_formats": ["md", "json"],
    "do_ocr": true,
    "force_ocr": true
  },
  "file_sources": [
    {
      "base64_string": "<pdf-base64>",
      "filename": "input.pdf"
    }
  ]
}
```

### 3. Wire Firecrawl to the local OCR adapter

Set these in the repo-root `.env`:

```bash
FIRE_PDF_ENABLE=true
FIRE_PDF_PERCENT=100
FIRE_PDF_BASE_URL=http://host.docker.internal:31337
FIRE_PDF_API_KEY=
PDF_RUST_EXTRACT_ENABLE=true
MINERU_PERCENT=0
RUNPOD_MU_API_KEY=
RUNPOD_MU_POD_ID=
```

Then recreate the API container:

```bash
docker compose up -d --force-recreate api
scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Important: `host.docker.internal` is the right URL from the Firecrawl API container to the host-side adapter under OrbStack.

Do not set `FIRE_PDF_API_KEY` unless the local adapter intentionally enforces one. The first local-only implementation should not require a key.

### 4. Protect OCR env from model profile changes

Review:

```text
scripts/firecrawl-ops/set_model_profile.sh
```

The model-profile switcher should not accidentally turn off a deliberately enabled local OCR setup. Adjust it so it either:

- preserves existing `FIRE_PDF_ENABLE`, `FIRE_PDF_PERCENT`, and `FIRE_PDF_BASE_URL` values, or
- accepts an explicit flag/profile for local OCR.

Preferred behavior:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
# keeps existing local OCR vars unless --reset-pdf-routing is passed
```

This matters because model profile changes are about Firecrawl's AI-backed extraction model (`OPENAI_BASE_URL`, `MODEL_NAME`, provider key). They should not silently disable local PDF OCR routing.

### 5. Add agent-facing CLI guidance

Update these docs after the adapter works:

- `.agents/skills/firecrawl-local-api/SKILL.md`
- `.agents/skills/firecrawl-ops/SKILL.md`
- `LOCAL_DEVELOPMENT_GUIDE.md`
- `docs/firecrawl-ops/references/ops-playbook.md`

Agents should learn:

- Use `--pdf-mode fast` for cheap text PDFs.
- Use `--pdf-mode auto` for normal work.
- Use `--pdf-mode ocr` when a scanned/image-only PDF needs local OCR.
- Plain markdown extraction does not use LLM credits.
- Local Docling OCR does not use Firecrawl cloud credits.
- Docling may use local CPU/RAM heavily and may download model assets on first use.

After updating skills, refresh the user-level copies/symlinks:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

### 6. Test the local OCR path

Start local services:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start-docling
scripts/firecrawl-ops/local_firepdf_ocr.sh start-adapter
scripts/firecrawl-ops/local_firepdf_ocr.sh health
```

Wire Firecrawl:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh env
docker compose up -d --force-recreate api
scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Run parse tests:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse apps/test-site/public/example.pdf \
  --formats markdown,html --pdf-mode auto --max-pages 5 \
  --out /tmp/firecrawl-docling-example-auto.json --pretty

scripts/firecrawl-ops/firecrawl_request.py parse apps/test-site/public/example.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 5 \
  --out /tmp/firecrawl-docling-example-ocr.json --pretty
```

Run the known scanned/image-only PDFs from the cross-repo regression folder if available:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse /path/to/scanned.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 \
  --out /tmp/firecrawl-docling-scanned-ocr.json --pretty
```

Compare:

- exit code
- markdown length
- table preservation
- section order
- figure captions
- failed pages
- pages processed
- processing time

### 7. Add a repeatable benchmark script

Created:

```text
scripts/firecrawl-ops/pdf_ocr_benchmark.py
```

It runs a small matrix:

- `fast`
- `auto`
- `ocr`
- local OCR enabled through Firecrawl's configured PDF route
- Docling backend through the FirePDF-compatible adapter
- PaddleOCR backend when implemented
- AI2 olmOCR backend when runtime allows

Sample categories:

- born-digital text PDF
- table-heavy financial/CRE report
- multi-column academic paper
- figure-heavy paper
- scanned/image-only PDF

Output should be JSON plus a small Markdown summary so agents can compare changes over time:

```text
tasks/tmp/firecrawl-pdf-ocr-benchmark-YYYYMMDD/
  summary.md
  results.json
  <sample>/<mode>/response.json
  <sample>/<mode>/fields/
```

## Acceptance Criteria

The local OCR setup is ready when:

- `GET /health` works on the adapter.
- Docling Serve health/API docs are reachable on `http://127.0.0.1:5001`.
- Firecrawl `POST /v2/parse` succeeds with `mode:"ocr"` for at least one scanned/image-only PDF that currently fails locally.
- Normal born-digital PDFs still work with `mode:"fast"` and `mode:"auto"`.
- The API container reaches the adapter through `host.docker.internal`.
- `.env` can enable/disable local OCR without code edits.
- No upstream-owned Firecrawl PDF engine files need modification.
- Skills and docs clearly explain local OCR costs, limits, startup, and model routing boundaries.
- `set_model_profile.sh` no longer accidentally disables local OCR routing.
- `scripts/firecrawl-ops/sync_agent_skills.sh` has been run after skill updates.

## Expected Limits

Local OCR is free in the Firecrawl-credit sense, but it is not free in machine cost:

- It uses local CPU/RAM/GPU/MPS resources.
- First run may download model assets.
- Large PDFs may be slow.
- Complex tables may still need engine comparison or downstream validation.
- Very large PDFs may hit Firecrawl's FirePDF size cap before reaching the adapter.

Do not claim perfect PDF understanding. Treat the adapter as a stronger local fallback, not a universal parser.

## Alternatives

### PaddleOCR PP-Structure

Use as the first serious benchmark alternative if Docling output is weak on tables or layout.

Pros:

- Apache-2.0
- Strong OCR/table/layout system
- Good fit for document-structure-heavy PDFs
- Supports service-style deployment patterns

Cons:

- Heavier integration
- More moving pieces than Docling
- May require more engine-specific normalization

Reference: https://github.com/PaddlePaddle/PaddleOCR

### AI2 olmOCR

Use as a high-power benchmark candidate if this Mac or another local machine has enough GPU/runtime support for a 7B-class VLM OCR stack.

Pros:

- Reputable source: Allen Institute for AI
- Apache-2.0
- Built specifically for PDF/document-image linearization into natural reading order
- Targets hard documents including tables, equations, handwriting, scans, and technical material
- CLI/toolkit model that can run against a local OpenAI-compatible inference server

Cons:

- Heavier than Docling Serve for a Mac/OrbStack default
- Usually wants a local GPU/VLM serving setup for practical throughput
- More model-serving complexity than a simple Docling Serve adapter
- Better as an optional `LOCAL_FIREPDF_ENGINE=olmocr` later, not the first implementation

References:

- https://github.com/allenai/olmocr
- https://olmocr.allenai.org/

### OCRmyPDF and Tesseract

Use as a low-cost fallback or pre-pass, not the main robust layout engine.

Pros:

- Mature and easy to run locally
- Good for adding searchable text layers to scanned PDFs
- Already partially installed locally: Tesseract and Poppler are present

Cons:

- Weak on tables, figures, reading order, and rich Markdown
- Produces OCR text, not a high-fidelity document structure

References:

- https://github.com/ocrmypdf/OCRmyPDF
- https://github.com/tesseract-ocr/tesseract

### Marker and Surya

Keep as experimental options, not default fork dependencies.

Pros:

- Strong document OCR/layout/table capabilities
- Good Markdown-oriented workflows

Cons:

- GPL-3.0 licensing and model/commercial terms are less comfortable as default fork infrastructure
- Better as local experiments or optional user-installed tools

References:

- https://github.com/datalab-to/marker
- https://github.com/datalab-to/surya

### MinerU

Defer unless Docling and PaddleOCR are not enough.

Pros:

- Very powerful PDF-to-Markdown/JSON stack
- Strong on formulas, figures, tables, and complex layouts
- Firecrawl already has a RunPod MinerU path conceptually

Cons:

- Heavy local runtime
- More awkward for Mac/OrbStack
- More operational burden than Docling Serve

Reference: https://github.com/opendatalab/MinerU

### Unstructured

Keep as a possible ETL-oriented fallback.

Pros:

- Mature document ingestion ecosystem
- Self-hostable API
- Table extraction options with high-resolution strategy

Cons:

- Less compelling than Docling or PaddleOCR for the specific goal of robust local PDF OCR/layout extraction
- More generic ingestion stack than focused FirePDF replacement

Reference: https://unstructured.readthedocs.io/

### Other Reputable Components Considered

These are useful to know about, but they should not displace the Docling-first adapter.

- Google-linked Tesseract: mature Apache-2.0 local OCR; best as fallback text OCR, not layout-aware Markdown.
- Microsoft Table Transformer: strong table detection/structure model; useful for table-specific benchmarks, but not a complete PDF OCR service.
- Apache Tika: mature Apache ingestion/parser stack with Tesseract OCR support; useful for broad file-type extraction, less compelling for rich PDF layout.
- GROBID: excellent scholarly PDF metadata, citations, and TEI extraction; not an OCR backend for scanned/image-only PDFs.
- LayoutParser: academic framework for document image layout analysis; useful building block, but not a ready local FirePDF service.
- docTR: Apache-2.0 deep-learning OCR library from Mindee; useful OCR component, but not enough by itself for robust PDF-to-Markdown with tables/figures.

References:

- https://github.com/tesseract-ocr/tesseract
- https://github.com/microsoft/table-transformer
- https://tika.apache.org/
- https://github.com/grobidOrg/grobid
- https://github.com/Layout-Parser/layout-parser
- https://github.com/mindee/doctr

## Decision

Proceed with Docling Serve plus a fork-owned FirePDF-compatible adapter.

Only after that baseline works, run the benchmark matrix against PaddleOCR PP-Structure and AI2 olmOCR where runtime allows. Promote another engine to an optional high-accuracy mode only if it materially beats Docling on the PDFs this fork actually cares about.
