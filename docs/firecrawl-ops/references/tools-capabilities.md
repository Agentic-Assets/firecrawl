# Firecrawl Docker Tools and Capabilities

## Core endpoints (self-hosted API)

Verified locally on 2026-08-13 after rebuilding the OrbStack Docker stack and testing the API, CLI/MCP wrappers, and parser canaries.

### `POST /v2/scrape`
- Best for: current typed scrape surface
- Typical output: markdown, html/rawHtml, links, images, summary, JSON, attributes/query
- Works locally for markdown, links, and metadata without model env
- Summary, JSON, query, and schema extraction need a valid model profile
- `actions` and screenshot formats require Fire Engine or browser-service support

### `POST /v2/parse`
- Best for: local file upload parsing
- Typical output: markdown, HTML, links, images, summary, JSON, metadata
- Works locally for HTML/PDF/DOCX/DOC/ODT/RTF/XLSX/XLS
- PDF markdown parsing verified with a 1-page fixture and a 25-page CRE market report
- Summary and JSON parse formats need valid model env
- PDF parser options: `parsers:[{"type":"pdf","mode":"auto|fast|ocr","maxPages":25}]`
- `PDF_RUST_EXTRACT_ENABLE=true` is the local default through compose; it improves simple text PDFs without credits
- `maxPages` constrains returned fallback text as well as metadata. Use a public fixture or the parser canary when verifying a page-boundary-sensitive workflow.
- Figure-heavy, table-heavy, scanned, or multi-column PDFs may still flatten on the default path
- Stronger local OCR/layout output is available through the fork's Docling-backed Fire PDF adapter: `scripts/firecrawl-ops/local_firepdf_ocr.sh start`, `enable-firecrawl`, then parse with `mode:"ocr"`
- Upstream page-level PDF markdown is intentionally not exposed by the local helper yet. It requires a Fire PDF `pages:[{page,markdown}]` response, while the local Docling adapter currently returns document-level markdown and QA metadata; requests fail explicitly rather than silently approximating page output.
- External Fire PDF or RunPod MinerU env can also be used, but those may spend that provider's budget

### `POST /v2/extract` + `GET /v2/extract/:id`
- Best for: async structured extraction from URLs
- Works locally with an explicit schema and a valid LLM profile
- Prefer v2 scrape `json` format for one-page extraction when possible

### `POST /v2/crawl/params-preview`
- Best for: converting a natural-language crawl instruction into crawl options
- LLM-backed; works locally with a valid LLM profile

### `GET /v2/team/queue-status` and `GET /v2/crawl/active`
- Best for: lightweight local runtime visibility
- Work with auth disabled

## CLI wrapper

Prefer:
```bash
scripts/firecrawl-ops/firecrawl_cli.sh <command> ...
```

From another repo or installed user-level skill, use:
```bash
~/.agents/skills/firecrawl-local-api/scripts/firecrawl_cli.sh <command> ...
```

The wrapper runs `npx -y firecrawl-cli@latest --api-url http://localhost:3002` and preserves the caller's current directory, so `parse ./report.pdf` resolves relative to where the agent is working. Verified commands:
- `scrape`
- `parse`
- `map`
- `search`
- `crawl` interactive submit and explicit status polling

Use `FIRECRAWL_CLI_PACKAGE=firecrawl-cli@1.18.0` if future `latest` releases break local behavior.

## Agent HTTP helper

The repo also includes a small dependency-free helper:

```bash
scripts/firecrawl-ops/firecrawl_request.py <health|scrape|search|map|crawl|crawl-status|parse|post> ...
```

Use it for local agent workflows, not app code. It fills gaps around predictable artifact saving and direct API options:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com \
  --formats markdown,links --out ./out/example.json \
  --save-fields ./out/example-fields --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html,images --pdf-mode auto --max-pages 25 \
  --out-dir ./out/firecrawl --save-fields ./out/report-fields --quiet

# Optional content-safety filtering for exploratory search. Omit --safe for the API default.
scripts/firecrawl-ops/firecrawl_request.py search "commercial real estate listings" \
  --limit 5 --safe true --scrape-formats markdown,links --out ./out/search.json

# Bounded agent automation without source bodies in the log.
scripts/firecrawl-ops/firecrawl_request.py health --metrics-only
scripts/firecrawl-ops/firecrawl_request.py crawl https://example.com \
  --limit 1 --scrape-formats markdown,links --wait --metrics-only
```

Do not duplicate official SDK behavior in production code. Use JS/Python/Go/Ruby/Rust/PHP/etc. SDKs there. Use the helper when an agent needs a shell-stable way to call the local API from another codebase. It preserves API envelopes by default, exposes payload-only output with `--unwrap`, and prevents source-body output with `--metrics-only`. `parse --max-pages` accepts only a positive cap. `crawl-status` and bounded `crawl --wait` exit nonzero on failed, cancelled, or timed-out jobs and save their final state when an output target is supplied.

`--safe` is deliberately opt-in. It is useful for exploratory agent searches but can reduce recall, so the governed CRE collector retains its source-specific acquisition paths and does not set it.

## Local verification tools

Use these when changing the local ops layer or proving the stack is ready:

```bash
scripts/firecrawl-ops/firecrawl_healthcheck.sh --evidence-dir tasks/tmp/firecrawl-healthcheck
scripts/firecrawl-ops/local_api_smoke_matrix.py
scripts/firecrawl-ops/local_capability_matrix.py
scripts/firecrawl-ops/pdf_parse_canary.py
scripts/firecrawl-ops/check_pnpm_docker_config.py
```

- `firecrawl_healthcheck.sh --evidence-dir` writes JSON and Markdown evidence with Docker status, API root response, scrape smoke output, image id, and pass/fail state.
- `local_api_smoke_matrix.py` probes the core local routes and writes JSON/Markdown under `tasks/tmp/local-api-smoke/`.
- `local_capability_matrix.py` compares `apps/api/src/routes/v2.ts`, this reference doc, and the latest smoke matrix to regenerate `local-capability-matrix.md`.
- `pdf_parse_canary.py` runs repeatable PDF parse canaries. Defaults to `fast,auto`; pass `--include-ocr` only when local OCR is configured.
- `check_pnpm_docker_config.py` is a CI-safe static guard for pnpm workspace config and Docker native dependency assumptions.

## Local Docling OCR adapter

For scanned/image-only PDFs or harder table/layout PDFs, use the local Fire PDF-compatible adapter:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor
scripts/firecrawl-ops/local_firepdf_ocr.sh enable-firecrawl
docker compose up -d --force-recreate api
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 --pretty
```

The adapter listens on `127.0.0.1:31337`, Docling Serve listens on `127.0.0.1:5001`, and the Firecrawl API container calls the adapter through `http://host.docker.internal:31337`. This does not use Firecrawl cloud credits. Earlier local tests had scanned/image PDFs succeed through `ocr` with the `research-page-aware` profile, but agents should benchmark unfamiliar PDFs and trust 422 quality failures or QA reports over blanket success claims. Profiles in `scripts/firecrawl-ops/pdf_ocr_profiles.json` expose common OCR/layout choices: `default`, `research-page-aware`, `tables-accurate`, `tables-fast`, `scanned-english`, `qa-debug`, and `figure-enrichment-lab`. List them with `scripts/firecrawl-ops/local_firepdf_ocr.sh profiles`; apply one with `restart-adapter --profile <name>`.

The local adapter returns explicit failure signals: `SCRAPE_PDF_OCR_BACKPRESSURE` / HTTP 429 when `LOCAL_FIREPDF_MAX_CONCURRENT_OCR` capacity is full, `SCRAPE_PDF_OCR_TIMEOUT` / HTTP 504 when Docling times out, and `SCRAPE_PDF_LOW_QUALITY` / HTTP 422 for mostly-empty or publisher-boilerplate OCR output. Successful Firecrawl parses can include `data.metadata.pdfOcr` with total chars, chars per page, empty-page ratio, and boilerplate ratios.

Tune adapter env vars such as `LOCAL_FIREPDF_TIMEOUT_SECONDS` (default 600), `LOCAL_FIREPDF_MAX_CONCURRENT_OCR` (default 2), `LOCAL_FIREPDF_FAIL_LOW_QUALITY` (default true), `LOCAL_FIREPDF_DOCLING_OCR_PRESET`, `LOCAL_FIREPDF_DOCLING_OCR_LANG`, `LOCAL_FIREPDF_DOCLING_PDF_BACKEND`, `LOCAL_FIREPDF_DOCLING_TABLE_MODE`, and `LOCAL_FIREPDF_DOCLING_TO_FORMATS`; run `scripts/firecrawl-ops/local_firepdf_ocr.sh settings` to print the full settings surface, then `restart-adapter` to apply adapter changes. `LOCAL_FIREPDF_DOCLING_MAX_SYNC_WAIT` (default 900) applies when starting or recreating Docling Serve. Explicit env vars override the named profile. Raw Docling JSON capture is available with `--capture-json` or the `qa-debug` profile and writes full-document debug artifacts under `tasks/tmp`.

Use `fast` for dense born-digital text PDFs when it succeeds; it can preserve more text than OCR and is much faster. Use `ocr` for scanned/image-only/slide-style PDFs. For unfamiliar document families, run the benchmark and read its `Recommended Mode` section.

For repeatable comparisons:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate \
  --max-pages 40 \
  --out-dir /tmp/firecrawl-pdf-ocr-benchmark \
  --strict
```

The benchmark now saves `fields/pages.jsonl`, `qa.json`, and `qa.md` per case when markdown is available, then recommends a mode/profile in root `summary.md`.

## Present but not configured locally

- `POST /v2/browser`, `GET /v2/browser`, `POST /v2/browser/:sessionId/execute` need `BROWSER_SERVICE_URL`.
- `POST /v2/agent` needs `EXTRACT_V3_BETA_URL`; an unconfigured local stack returns HTTP 503 with that explicit prerequisite.
- `POST /v1/deep-research` starts locally, but it is slower and may keep processing for several minutes.
- CLI `agent` and `interact` need the corresponding backend services/model configuration.
- CLI `crawl --wait` may hang locally. Use `firecrawl_request.py crawl --wait`, which polls `/v2/crawl/:id` with an explicit timeout.

## Upstream-only (not verified self-hosted)

- `POST /v2/monitor` and related monitor endpoints: page-change monitoring with optional LLM judgment; requires cloud billing and judge configuration. Not part of the local ops stack yet.

## Practical selection guide

- Need one page quickly -> `v2/scrape`
- Need candidate URLs first -> `v2/map` or `v2/search`; map first for JS/news hubs
- Need many pages -> `v2/crawl`, then poll status through an SDK or `firecrawl_request.py crawl --wait`
- Need a local file parsed -> `v2/parse` or CLI `parse`
- Need structured fields/entities from one page -> `v2/scrape` with `json`
- Need async structured fields/entities from multiple pages -> `v2/extract`
- Need RSS/Atom items -> native HTTP plus an XML feed parser, not generic scrape

Search backend selection is Fire Engine when configured, then SearxNG when configured, then DuckDuckGo HTML. Search is discovery only: hit ranks can vary and must not be treated as reproducible source evidence. The CRE collector remains governed by its source-specific SDK/API contracts and does not use the generic helper as a collection path.

## Runtime stack (docker compose)

Typical services:
- `api`
- `playwright-service`
- `redis`
- `rabbitmq`
- `nuq-postgres`

Health check baseline:
- API reachable at `http://localhost:3002/`
- smoke scrape returns `success: true`
- Optional evidence mode writes to `tasks/tmp/firecrawl-healthcheck/`
- Queue status is the authority for effective local concurrency. The upstream fallback is `maxConcurrency: 2`; do not raise it without a separate OrbStack resource proof.
