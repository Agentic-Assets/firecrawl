# Firecrawl Local Development Guide

This guide describes this fork's local self-hosted setup. It is intentionally focused on the current Mac workflow: OrbStack, Docker compose, the v2 API, local agent skills, and model-profile helpers.

## Fresh Clone Quickstart

Use this path on a new Mac or for a business partner joining the project.

Prerequisites:
- OrbStack installed and running.
- Git installed and authenticated for this repository.
- `python3`, `curl`, and `bash` available from macOS.
- Optional but useful: Node.js/npm for the local Firecrawl CLI wrapper, and `jq` for reading JSON output.

```bash
git clone git@github.com:Agentic-Assets/firecrawl.git
cd firecrawl

# Confirm Docker is going through OrbStack.
docker context show
docker compose version

# Create a gitignored local .env with sane local defaults.
scripts/firecrawl-ops/set_model_profile.sh budget

# Optional but recommended for local agents and commit reminders.
scripts/firecrawl-ops/install_git_hooks.sh
scripts/firecrawl-ops/sync_agent_skills.sh

# Build and start the self-hosted stack.
docker compose build
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Expected result:
- `docker context show` prints `orbstack`.
- `http://localhost:3002/` returns the Firecrawl API message.
- `docker compose ps` shows `api`, `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres` up.

If `docker context show` does not print `orbstack`, open OrbStack and run:

```bash
docker context use orbstack
```

The generated root `.env` is local-only and gitignored. Non-AI scrape, crawl, map, search, and basic PDF parse can work without model credits. Add a provider key to `.env` only when you need AI-backed summary, JSON extraction, query, or `/v2/extract`.

## 1. Local Runtime

Start from the repo root:

```bash
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Expected baseline:
- OrbStack is running and `docker context show` is `orbstack`.
- API is reachable at `http://localhost:3002`.
- Core services include `api`, `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres`.
- Local auth is usually disabled with `USE_DB_AUTHENTICATION=false`.

Useful commands:

```bash
docker compose ps
docker compose logs api --tail 200
docker compose logs playwright-service --tail 200
docker compose up -d --force-recreate api
docker compose down
```

## 2. Env Files

- `./.env` is the primary local env file read by Docker compose. It is gitignored; never commit secrets.
- `apps/api/.env.example` is upstream's reference list of available variables.
- `apps/api/.env.local` is tracked upstream scaffolding and is not the Docker compose env file.

Create or refresh local model defaults:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
docker compose up -d --force-recreate api
```

Important fork/local vars:

| Var | Purpose |
| :--- | :--- |
| `FIRECRAWL_API_URL` | Local helper/CLI target, normally `http://localhost:3002` |
| `OPENAI_API_KEY` | Provider key for OpenRouter, Vercel AI Gateway, or OpenAI-compatible providers |
| `OPENAI_BASE_URL` | Provider base URL written by `set_model_profile.sh` |
| `MODEL_NAME` | Firecrawl's internal LLM model id |
| `OPENROUTER_API_KEY` | Optional direct OpenRouter path; not the default local profile route |
| `PDF_RUST_EXTRACT_ENABLE` | Local PDF text extraction; compose defaults it to `true` |
| `FIRE_PDF_BASE_URL` / `FIRE_PDF_API_KEY` | Optional Fire PDF-compatible OCR/layout service. For the local Docling adapter, use `FIRE_PDF_BASE_URL=http://host.docker.internal:31337` and leave `FIRE_PDF_API_KEY` empty |
| `RUNPOD_MU_API_KEY` / `RUNPOD_MU_POD_ID` | Optional external MinerU-style OCR/layout service |
| `SWARM_SUPABASE_URL` / `SWARM_SUPABASE_KEY` | Optional swarm telemetry destination |

Profiles:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget        # OpenRouter DeepSeek V4 Flash
scripts/firecrawl-ops/set_model_profile.sh escalated     # OpenRouter DeepSeek V4 Pro
scripts/firecrawl-ops/set_model_profile.sh gateway       # Vercel AI Gateway DeepSeek V4 Flash
scripts/firecrawl-ops/set_model_profile.sh gateway-codex # Vercel AI Gateway OpenAI model
scripts/firecrawl-ops/set_model_profile.sh openai-direct # OpenAI Platform
```

The local CLI/helper can apply those same profiles before an AI-backed call:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh --firecrawl-model-profile budget --firecrawl-healthcheck \
  scrape https://example.com --format summary --json --pretty

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown --query "What is this document about?" \
  --model-profile escalated --healthcheck --pretty
```

The wrapper profile flags update `.env` and recreate the API container by default. They matter for summary, query, JSON extraction, and `/v2/extract`. Plain PDF markdown extraction uses the local PDF parser and does not call the LLM model. Existing local OCR routing vars are preserved unless explicitly overwritten.

## 3. Local API Quick Use

Prefer v2 endpoints for new local work:

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","formats":["markdown","links"]}'
```

PowerShell form:

```powershell
$body = @{
  url = "https://example.com"
  formats = @("markdown", "links")
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "http://localhost:3002/v2/scrape" -Method Post -ContentType "application/json" -Body $body
```

Endpoint selection:
- One URL: `POST /v2/scrape`
- Search: `POST /v2/search`
- Discover URLs: `POST /v2/map`
- Crawl site: `POST /v2/crawl`, then poll `GET /v2/crawl/:id`
- Many URLs: `POST /v2/batch/scrape`, then poll status
- Local files: `POST /v2/parse`
- Structured extraction: `POST /v2/scrape` with JSON format, or `POST /v2/extract` with explicit schema

## 4. CLI And Agent Helpers

Use the upstream Firecrawl CLI through the local wrapper:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o ./out/example.json
scripts/firecrawl-ops/firecrawl_cli.sh --firecrawl-model-profile budget --firecrawl-healthcheck \
  scrape https://example.com --format summary --json --pretty
```

From another codebase, use the installed skill copy:

```bash
~/.agents/skills/firecrawl-local-api/scripts/firecrawl_cli.sh parse ./report.pdf --json --pretty
```

Use the direct helper when an agent needs saved field artifacts or advanced PDF parser options:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com \
  --formats markdown,links --pretty --out ./out/example.json \
  --save-fields ./out/example-fields --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html,images --pdf-mode auto --max-pages 25 \
  --out-dir ./out/firecrawl --save-fields ./out/report-fields --pretty --quiet
```

Use official Firecrawl SDKs in application code. The helper is for local agent runs from any codebase on this computer.

For local crawls, avoid `--wait` if it hangs; submit and poll:

```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

## 5. PDF Parsing

Local PDF parsing does not spend Firecrawl cloud credits. `creditsUsed` in local responses is local accounting metadata. External OCR/layout providers can still have their own costs if configured.

Direct HTTP with parser knobs:

```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown","html"],"parsers":[{"type":"pdf","mode":"auto","maxPages":25}]}' \
  -F "file=@./report.pdf"
```

Modes:
- `auto`: default; local Rust extraction for text PDFs, then configured OCR fallbacks, then `pdf-parse`
- `fast`: local text extraction only; avoids OCR-style work
- `ocr`: useful when the local Docling Fire PDF-compatible adapter, hosted Fire PDF, or MinerU-style services are configured

The fully local path is strongest for text PDFs. Tables, figures, scans, and complex multi-column layouts can still flatten into markdown.

### Local Docling OCR adapter

This fork can run a local Fire PDF-compatible adapter backed by Docling Serve. It is local and does not spend Firecrawl cloud credits.

Start the OCR services. Use `research-page-aware` for academic papers when page chunks/citations matter:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor --smoke-pdf ./report.pdf
```

Wire the running Firecrawl API to the adapter:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh enable-firecrawl
docker compose up -d --force-recreate api
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Use OCR mode for scanned/image-only PDFs:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 --pretty
```

Operational notes:

- Docling Serve runs in OrbStack/Docker on `127.0.0.1:5001`.
- The helper pins the known-good Docling Serve CPU image by digest and sets `DOCLING_SERVE_MAX_SYNC_WAIT=900` on new starts. Override `LOCAL_FIREPDF_DOCLING_IMAGE` or `LOCAL_FIREPDF_DOCLING_MAX_SYNC_WAIT` only when intentionally testing runtime changes.
- The local Fire PDF adapter runs on `127.0.0.1:31337`.
- Firecrawl's API container reaches the adapter with `FIRE_PDF_BASE_URL=http://host.docker.internal:31337`.
- `--pdf-mode fast` avoids OCR; `--pdf-mode auto` uses Firecrawl's normal PDF decision path; `--pdf-mode ocr` forces the local Docling adapter when configured.
- Mode choice is document-dependent. In a local 2026-05-23 stress test, a 40-page born-digital spec was much richer and faster in `fast`, while a 25-page encrypted slide-style market report benefited modestly from Docling OCR. Earlier scanned/image research tests succeeded through Docling OCR with `research-page-aware` page markers, but later paper batches exposed low-quality publisher-boilerplate cases. Use `fast` for dense born-digital text, `ocr` for scanned/image-only/slide-style PDFs, and benchmark unfamiliar document families.
- Named profiles live in `scripts/firecrawl-ops/pdf_ocr_profiles.json`. List them with `scripts/firecrawl-ops/local_firepdf_ocr.sh profiles`.
- Recommended profiles: `default`, `research-page-aware`, `tables-accurate`, `tables-fast`, `scanned-english`, `qa-debug`, and `figure-enrichment-lab`.
- Apply a profile with `scripts/firecrawl-ops/local_firepdf_ocr.sh restart-adapter --profile tables-accurate`. Use `start --profile <name>` for first start.
- Raw Docling JSON capture is off unless the profile enables it or you pass `--capture-json`; it writes sensitive full-document artifacts under `tasks/tmp/firecrawl-docling-debug` by default.
- The adapter limits OCR concurrency with `LOCAL_FIREPDF_MAX_CONCURRENT_OCR` (default 2). Extra simultaneous calls return `SCRAPE_PDF_OCR_BACKPRESSURE` / HTTP 429 instead of overloading Docling.
- Docling timeouts return `SCRAPE_PDF_OCR_TIMEOUT` / HTTP 504. Raise `LOCAL_FIREPDF_TIMEOUT_SECONDS` or `LOCAL_FIREPDF_DOCLING_MAX_SYNC_WAIT` only after checking adapter and Docling logs.
- Low-quality OCR output with mostly empty pages or publisher/license boilerplate is rejected by default with `SCRAPE_PDF_LOW_QUALITY` / HTTP 422. Set `LOCAL_FIREPDF_FAIL_LOW_QUALITY=false` only when collecting diagnostics.
- Dynamic adapter knobs are accepted as env overrides before `start-adapter` / `restart-adapter` / `start`: `LOCAL_FIREPDF_TIMEOUT_SECONDS` (default 600), `LOCAL_FIREPDF_MAX_CONCURRENT_OCR`, `LOCAL_FIREPDF_FAIL_LOW_QUALITY`, `LOCAL_FIREPDF_DOCLING_OCR_PRESET`, `LOCAL_FIREPDF_DOCLING_OCR_LANG`, `LOCAL_FIREPDF_DOCLING_PDF_BACKEND`, `LOCAL_FIREPDF_DOCLING_TABLE_MODE`, `LOCAL_FIREPDF_DOCLING_TO_FORMATS`, and optional enrichment flags. `LOCAL_FIREPDF_DOCLING_MAX_SYNC_WAIT` (default 900) applies when starting or recreating Docling Serve. Explicit env vars override the named profile.
- Print the full tunable settings surface with `scripts/firecrawl-ops/local_firepdf_ocr.sh settings`.
- Quick OCR verification: `scripts/firecrawl-ops/local_firepdf_ocr.sh smoke ./report.pdf`.
- Repeatable PDF checks can use `scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf --modes fast,auto,ocr --profiles default,research-page-aware,tables-accurate --max-pages 40 --out-dir /tmp/firecrawl-pdf-ocr-benchmark --strict`. The saved `summary.md` includes accept/reject/manual-review guidance plus a recommended mode/profile per PDF, and each case saves `fields/pages.jsonl`, `qa.json`, and `qa.md` when output is available.
- Successful local OCR responses expose stable `data.metadata.pdfOcr` fields: active profile, settings fingerprint, resolved Docling options, page-boundary source, compact per-page quality summaries, boilerplate families/scores, table/figure JSON signals, and low-quality gate settings. OCR-mode FirePDF cache is bypassed so profile/env changes cannot reuse stale OCR output.
- Direct adapter tests may include a `docling_options` object in `POST /ocr`; Firecrawl API calls use the adapter container env.
- Stop services with `scripts/firecrawl-ops/local_firepdf_ocr.sh stop`.

## 6. Cross-Agent Tooling

Keep these layers separate:

1. Firecrawl local runtime: OrbStack + Docker compose, API at `http://localhost:3002`.
2. Reusable tool interfaces: direct HTTP API, `firecrawl_cli.sh`, `firecrawl_request.py`, and `firecrawl_mcp.sh`.
3. Agent adapters: `.cursor/mcp.json`, `.cursor/skills/`, `.agents/skills/`, or any MCP-capable client config.
4. Agent model runtime: Cursor Composer, Codex, Claude, or another model.

For MCP-capable agents:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

Cursor is one optional adapter. Cursor SDK code does not load project settings by default; pass `mcpServers` inline or use `local: { cwd: process.cwd(), settingSources: ["project"] }`. Cursor cloud agents cannot reach this Mac's `localhost:3002` unless the API is exposed at a reachable URL.

Firecrawl's own AI-backed formats still use `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `MODEL_NAME`. Do not treat Cursor Composer as Firecrawl's internal model provider unless Cursor publishes an OpenAI-compatible endpoint.

## 7. Skill Sync

After editing repo skills or local-agent docs:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

It copies `firecrawl-ops` and `firecrawl-local-api` into `~/.agents/skills` and symlinks them into `~/.codex/skills`, `~/.claude/skills`, and `~/.cursor/skills`.

Enable advisory git hook reminders per clone:

```bash
scripts/firecrawl-ops/install_git_hooks.sh
```

## 8. Upstream Sync

Keep fork-owned ops assets in `.agents/`, `docs/firecrawl-ops/`, `scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, and `AGENTS.md`. Sync upstream on a branch:

```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

Prefer upstream for product/API/SDK/security files. Prefer this fork for local ops, skills, model-routing docs, and self-hosted workflow docs.
