---
name: firecrawl-local-api
description: Call the local self-hosted Firecrawl API at http://localhost:3002 directly or through the Firecrawl CLI. Use when the user wants page scraping, search, map, crawl, batch scrape, local file parsing including PDF/DOCX/XLSX, structured extraction, CLI examples, endpoint selection, local method testing, or guidance on formats/settings for this fork's local API.
---

# Firecrawl Local API

Use this skill to call the local Firecrawl API directly or through the Firecrawl CLI. Use `firecrawl-ops` first when Docker health, OrbStack, model config, rebuilds, upstream sync, or logs matter.

## Assumptions

- Base URL: `http://localhost:3002`
- Auth: currently disabled with `USE_DB_AUTHENTICATION=false`; do not send a bearer token unless `.env` later sets `TEST_API_KEY`.
- Verification date: 2026-05-23 after upstream sync and OrbStack rebuild.
- Cloud credits are not charged when hitting this local API. `creditsUsed` is local accounting metadata. Third-party costs can still occur for AI providers, proxies, or hosted search integrations.
- Root `.env` may be absent. Non-AI scrape/map/search/parse can still work; AI formats need `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL_NAME`.
- The repo usually lives at `/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl`. If an agent is working from another codebase, direct HTTP calls still work, and installed helper scripts are available under `~/.agents/skills/firecrawl-local-api/scripts/`.
- Prefer upstream-maintained interfaces first: direct API, official SDKs, or `firecrawl_cli.sh`. Use `firecrawl_request.py` only for local agent ergonomics such as advanced PDF parser options and saving split artifacts.

## What Works Locally

| Endpoint | Status | Use |
|---|---:|---|
| `POST /v2/scrape` | works | Single URL to markdown/html/rawHtml/links/images/summary/json/attributes/query. |
| `POST /v2/search` | works | Web search; `scrapeOptions` can enrich hits with markdown. |
| `POST /v2/map` | works | URL discovery. |
| `POST /v2/crawl` + `GET /v2/crawl/:id` | works | Async crawl with status polling. CLI `--wait` may hang locally; poll by id. |
| `POST /v2/batch/scrape` + `GET /v2/batch/scrape/:id` | works | Async scrape of many URLs. |
| `POST /v2/parse` | works | Multipart upload for local HTML/PDF/DOCX/DOC/ODT/RTF/XLSX/XLS. PDF parser options include `mode` and `maxPages`. |
| `POST /v2/extract` + `GET /v2/extract/:id` | works with schema | Async structured extraction. Provide an explicit schema. |
| `POST /v2/crawl/params-preview` | works | LLM-backed natural-language crawl options. |
| `GET /v2/team/queue-status` | works | Local queue visibility. |
| `GET /v2/crawl/active` | works | Active crawl listing. |
| `POST /v1/extract` | works with schema | Synchronous structured extraction when schema is explicit. |
| `POST /v1/llmstxt` + `GET /v1/llmstxt/:id` | works | Generate llms.txt style output. |

Prefer v2 endpoints for new work unless the user specifically asks for v1.

## Not Configured Locally

- `POST /v2/browser`, `GET /v2/browser`, and `POST /v2/browser/:sessionId/execute`: need `BROWSER_SERVICE_URL`.
- `POST /v2/agent`: needs `EXTRACT_V3_BETA_URL`.
- Scrape `actions`, screenshot formats, and scrape-browser interaction: need Fire Engine or browser-service support.
- Prompt-only extract/schema generation may fail on weaker budget models; provide an explicit schema.
- Summary, JSON extraction, query, params-preview, and other AI formats fail until model env is configured.

## LLM-Backed Calls

The local stack reads root `.env`. Change model profiles with:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/set_model_profile.sh escalated
scripts/firecrawl-ops/set_model_profile.sh gateway
scripts/firecrawl-ops/set_model_profile.sh gateway-codex
scripts/firecrawl-ops/set_model_profile.sh openai-direct
docker compose up -d --force-recreate api
```

The local wrappers can also apply a profile before a call:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh --firecrawl-model-profile budget --firecrawl-healthcheck \
  parse ./report.pdf --format markdown --json --pretty

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown --query "What is this document about?" \
  --model-profile escalated --healthcheck --pretty
```

This changes the running Firecrawl API container's model env. It matters for AI-backed formats such as `summary`, `query`, JSON extraction, and `/v2/extract`; plain PDF markdown extraction does not use the LLM model.

For OpenRouter and Vercel AI Gateway, put the provider key in `OPENAI_API_KEY`. The helper sets:

- `OPENAI_BASE_URL=https://openrouter.ai/api/v1`
- `MODEL_NAME=deepseek/deepseek-v4-flash`

Use DeepSeek V4 Flash as the primary low-cost model. Escalate to `deepseek/deepseek-v4-pro` for noisy pages, low-confidence fields, or repeated malformed output. If LLM-backed calls fail, check API logs for provider/model errors before blaming the endpoint.

## CLI Patterns

Prefer the fork wrapper. It runs the upstream Firecrawl CLI against the local API, so agents get the maintained CLI surface without cloud defaults. From this repo, use:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o ./out/example.json
```

From another repo or an installed user-level skill, use:

```bash
~/.agents/skills/firecrawl-local-api/scripts/firecrawl_cli.sh parse ./report.pdf --json --pretty
~/.agents/skills/firecrawl-local-api/scripts/firecrawl_healthcheck.sh
```

Set `FC_DIR=/path/to/firecrawl` for repo-dependent helper scripts when the repo is not in the usual location. The CLI wrapper preserves the caller's current directory, so relative file paths like `./report.pdf` resolve from wherever the agent ran the command. It runs `npx -y firecrawl-cli@latest --api-url http://localhost:3002`. Override with `FIRECRAWL_CLI_PACKAGE=firecrawl-cli@1.18.0` if a future latest release breaks.

Wrapper-only CLI options must come before the Firecrawl command:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh --firecrawl-model-profile budget --firecrawl-healthcheck \
  scrape https://example.com --format summary --json --pretty
```

## Agent HTTP Helper

Use `firecrawl_request.py` when you need direct API control, predictable saved outputs, or PDF parser options that the CLI does not expose. It has no third-party Python dependency and works from any current directory:

```bash
~/.agents/skills/firecrawl-local-api/scripts/firecrawl_request.py scrape https://example.com \
  --formats markdown,links --pretty --out ./out/example.json \
  --save-fields ./out/example-fields --quiet --print-paths

~/.agents/skills/firecrawl-local-api/scripts/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html,images --pdf-mode auto --max-pages 25 \
  --out-dir ./out/firecrawl --save-fields ./out/report-fields --pretty --quiet
```

Selection rule:

- Use CLI for normal `scrape`, `parse`, `search`, `map`, `crawl`, config/setup, and anything listed in `firecrawl_cli.sh --help`.
- Use `firecrawl_request.py parse` for `--pdf-mode`, `--max-pages`, `--no-pdf-parse`, `--fire-pdf-async`, or split artifact saving.
- Use official SDKs in app code instead of shelling out.
- Use raw `curl` when debugging exact wire payloads.

## Cross-Agent MCP

The reusable local MCP entrypoint is:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

It runs the upstream `firecrawl-mcp` package against `FIRECRAWL_API_URL=http://localhost:3002`. MCP-capable agents should use this wrapper rather than duplicating package/env setup in each client config.

Cursor is only one optional adapter:

- `.cursor/mcp.json` registers `firecrawl-local` by calling `scripts/firecrawl-ops/firecrawl_mcp.sh`.
- `.cursor/skills/firecrawl-local-api/SKILL.md` gives Cursor/Composer local API guidance.
- `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` explains the reusable MCP/CLI/API layer separately from Cursor Composer.
- `scripts/firecrawl-ops/sync_agent_skills.sh` copies these repo skills into `~/.agents/skills` and symlinks them into user-level agent skill folders.

Composer 2.5 is an agent runtime/model choice. It is not Firecrawl's internal LLM backend unless Cursor provides an OpenAI-compatible model endpoint. Cursor SDK local agents should pass the MCP server inline or opt into project settings with `local.settingSources`; cloud agents cannot reach this Mac's `localhost:3002` unless Firecrawl is exposed at a reachable URL.

For crawl, avoid `--wait` locally:

```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

## Curl Patterns

Plain scrape:

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","formats":["markdown","links"]}'
```

Structured one-page extraction:

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","formats":["markdown",{"type":"json","prompt":"Extract the domain and page heading.","schema":{"type":"object","properties":{"domain":{"type":"string"},"heading":{"type":"string"}},"required":["domain","heading"]}}]}'
```

Local file parse:

```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown"],"parsers":["pdf"]}' \
  -F "file=@./report.pdf"
```

Use `{"type":"summary"}` or `{"type":"json"}` only after model env is configured.

PDF parser controls:

```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown","html"],"parsers":[{"type":"pdf","mode":"auto","maxPages":25}]}' \
  -F "file=@./report.pdf"
```

Supported parser modes are `auto`, `fast`, and `ocr`. Use `auto` by default. Use `fast` when you want local text extraction without OCR-style work. Use `ocr` when Fire PDF, the local Docling adapter, or MinerU-style OCR services are configured; without one of those OCR backends, `ocr` is not meaningfully stronger than the fallback parser. `maxPages` caps PDF pages processed, up to 10000.

Equivalent helper form:

```bash
~/.agents/skills/firecrawl-local-api/scripts/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode auto --max-pages 25 --pretty
```

PDF output reality:

- The default local path is strongest for text-based PDFs. With `PDF_RUST_EXTRACT_ENABLE=true`, Rust extraction handles simple text PDFs locally and falls back when the layout is complex.
- Figure-heavy, table-heavy, scanned, or multi-column PDFs may flatten on the default path; start the local Docling adapter for stronger local OCR/layout extraction.
- `formats:["images"]` only returns images when the parsed HTML/markdown exposes image tags; many PDFs return an empty image list.
- `formats:["html"]` may be markdown-derived HTML, not a faithful page layout with `<table>` or `<img>` tags.
- For dense born-digital PDFs, `fast` can be both faster and richer than OCR. A local 40-page spec test produced much more markdown in `fast` than in `auto`/`ocr`. Use OCR for scanned/image-only/slide-style documents, and benchmark unfamiliar document families before committing to one mode.
- Stronger local OCR/layout extraction uses Fire PDF-compatible OCR routing. This fork provides a local Docling adapter:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor
scripts/firecrawl-ops/local_firepdf_ocr.sh enable-firecrawl
docker compose up -d --force-recreate api
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 --pretty
```

The local Docling adapter does not spend Firecrawl cloud credits. In current local tests, `research-page-aware` OCR successfully parsed known scanned/image PDFs and produced page-aware markdown. External Fire PDF or RunPod MinerU backends can still spend their provider budget.

Named OCR profiles live in `scripts/firecrawl-ops/pdf_ocr_profiles.json`. List them with `scripts/firecrawl-ops/local_firepdf_ocr.sh profiles`. Useful profiles: `research-page-aware` for academic page chunks, `tables-accurate` for table-heavy papers, `scanned-english` for image-only English scans, and `qa-debug` when raw Docling JSON is needed. Apply a changed profile with `scripts/firecrawl-ops/local_firepdf_ocr.sh restart-adapter --profile <name>`.

Useful adapter tuning env vars before `scripts/firecrawl-ops/local_firepdf_ocr.sh start-adapter` / `start`: `LOCAL_FIREPDF_TIMEOUT_SECONDS` (default 600), `LOCAL_FIREPDF_DOCLING_OCR_PRESET`, `LOCAL_FIREPDF_DOCLING_OCR_LANG`, `LOCAL_FIREPDF_DOCLING_PDF_BACKEND`, `LOCAL_FIREPDF_DOCLING_TABLE_MODE`, `LOCAL_FIREPDF_DOCLING_TO_FORMATS`, and optional enrichment flags. Explicit env vars override the named profile. Run `scripts/firecrawl-ops/local_firepdf_ocr.sh settings` to print the full settings surface, then `restart-adapter` to apply changes. Use `scripts/firecrawl-ops/local_firepdf_ocr.sh smoke ./report.pdf` for a one-command OCR parse check. For a saved comparison matrix with per-PDF recommendations, page chunks, and QA reports:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate \
  --max-pages 40 \
  --out-dir /tmp/firecrawl-pdf-ocr-benchmark \
  --strict
```

Async extract with schema:

```bash
ID=$(curl -sS -X POST http://localhost:3002/v2/extract \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://example.com"],"prompt":"Extract the page heading.","schema":{"type":"object","properties":{"heading":{"type":"string"}},"required":["heading"]},"enableWebSearch":false}' \
  | jq -r .id)
curl -sS "http://localhost:3002/v2/extract/$ID"
```

Search with scraped snippets:

```bash
curl -sS -X POST http://localhost:3002/v2/search \
  -H "Content-Type: application/json" \
  -d '{"query":"firecrawl docs","limit":3,"scrapeOptions":{"formats":["markdown"]}}'
```

## Choosing Quickly

- Need one page text: `v2/scrape` with `formats:["markdown"]`.
- Need fields from one page: `v2/scrape` with a `json` format.
- Need fields from multiple URLs: `v2/extract` with schema.
- Need a PDF or Office file on disk: `v2/parse`.
- Need a site inventory: `v2/map`.
- Need many pages: `v2/crawl` or `v2/batch/scrape`.
- Need current runtime state: use `firecrawl-ops`, then `v2/team/queue-status`.
