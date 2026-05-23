---
name: firecrawl-ops
description: Operate, verify, sync, and troubleshoot the self-hosted Firecrawl stack in this fork. Use when the user asks about OrbStack/Docker compose runtime health, local API startup/rebuilds/logs, Firecrawl CLI local setup, upstream sync from firecrawl/firecrawl:main, endpoint capability checks, model routing, OpenRouter/Vercel AI Gateway/OpenAI profile changes, or which Firecrawl methods work locally.
---

# Firecrawl Ops

Use this skill for runtime, sync, and platform work around this fork's self-hosted Firecrawl stack. For directly calling scrape/search/parse endpoints, pair it with `firecrawl-local-api`.

## First Checks

Run from the repo root unless `FC_DIR` is set:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
docker compose ps
```

This Mac uses OrbStack, not Docker Desktop. If Docker commands fail, open OrbStack and confirm `docker context show` is `orbstack`. The expected local API is `http://localhost:3002`.

## Current Local Reality

Verified on 2026-05-23 after syncing `firecrawl/firecrawl:main` and rebuilding with OrbStack:

- Core stack works: `api`, `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres`.
- NuQ Postgres must have schema table `nuq.queue_scrape`; compose now waits for it via healthcheck.
- Local auth is disabled when `USE_DB_AUTHENTICATION=false`; no bearer token is required.
- Root `.env` is gitignored and may not exist. Non-AI scrape/map/search/parse work without it; AI-backed summary/json/query/extract need provider env.
- The local Firecrawl CLI path works through `scripts/firecrawl-ops/firecrawl_cli.sh` or `FIRECRAWL_API_URL=http://localhost:3002 npx -y firecrawl-cli@latest ...`.
- The upstream CLI is the default for broad command coverage. Use `scripts/firecrawl-ops/firecrawl_request.py` only when an agent needs dependency-free direct HTTP, advanced `/v2/parse` PDF parser options, or split saved artifacts such as markdown/html/metadata files.
- User-level installed helper scripts also work from other repos at `~/.agents/skills/firecrawl-ops/scripts/`. Set `FC_DIR=/path/to/firecrawl` if the repo is not in the usual local checkout path.
- The CLI `crawl --wait` can hang locally even after the API finishes. Prefer submit then status-poll by job id.
- PDF Rust extraction is enabled by default through compose when `PDF_RUST_EXTRACT_ENABLE` is unset. This improves simple text-based PDFs locally but does not turn scanned/table-heavy PDFs into full layout-aware output.

Model routing:

- Use `scripts/firecrawl-ops/set_model_profile.sh <profile>` to write `OPENAI_BASE_URL` and `MODEL_NAME` in root `.env`.
- The helper also keeps local PDF defaults in `.env`: `PDF_RUST_EXTRACT_ENABLE=true`, `PDF_SHADOW_COMPARISON_ENABLE=false`, `MINERU_PERCENT=0`, and `FIRE_PDF_PERCENT=10`. Existing local OCR routing vars are preserved when switching model profiles.
- `firecrawl_cli.sh` and `firecrawl_request.py` can apply a model profile before a call. This recreates the API container by default so AI-backed formats see the new `OPENAI_BASE_URL` / `MODEL_NAME`.
- For OpenRouter and Vercel AI Gateway profiles, put the provider key in `OPENAI_API_KEY`; these profiles use OpenAI-compatible base URLs.
- `OPENROUTER_API_KEY` exists in API config but is not the default path for these local profiles.
- Use model IDs exactly as provider IDs, without an extra `openrouter/` prefix.

Known local gaps:

- `POST /v2/browser` and `/v2/browser/:sessionId/execute` are registered but need `BROWSER_SERVICE_URL`.
- `POST /v2/agent` is registered but needs `EXTRACT_V3_BETA_URL`.
- Scrape `actions`, screenshot formats, and scrape-browser interaction need Fire Engine or browser-service support.
- AI-backed parse/scrape summary and JSON fail until `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL_NAME` are valid.

## Local CLI

Prefer the wrapper so agents do not forget the local API URL. It is still the upstream Firecrawl CLI, just pinned to the local API:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o ./out/example.json
scripts/firecrawl-ops/firecrawl_cli.sh --firecrawl-model-profile budget --firecrawl-healthcheck \
  scrape https://example.com --format summary --json --pretty
```

From another repo, use the installed copy:

```bash
~/.agents/skills/firecrawl-ops/scripts/firecrawl_cli.sh parse ./report.pdf --json --pretty
FC_DIR=/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl ~/.agents/skills/firecrawl-ops/scripts/firecrawl_healthcheck.sh
```

Equivalent raw form:

```bash
FIRECRAWL_API_URL=http://localhost:3002 npx -y firecrawl-cli@latest scrape https://example.com
```

The CLI wrapper preserves the caller's current directory, so relative upload paths work for commands such as `parse ./report.pdf`. The CLI supports `scrape`, `crawl`, `map`, `parse`, `search`, `agent`, `interact`, `monitor`, setup/config commands, and output flags. For local crawl jobs, use:

```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

## Agent HTTP Helper

Use `scripts/firecrawl-ops/firecrawl_request.py` when the upstream CLI is too high-level for an agent task. It uses only Python stdlib, reads `FIRECRAWL_API_URL` / `FIRECRAWL_API_KEY`, preserves caller paths, and supports `--out`, `--out-dir`, and `--save-fields`:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com \
  --formats markdown,links --pretty --out ./out/example.json \
  --save-fields ./out/example-fields --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html,images --pdf-mode auto --max-pages 25 \
  --out-dir ./out/firecrawl --save-fields ./out/report-fields --pretty --quiet

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown --query "What is this document about?" \
  --model-profile escalated --healthcheck --pretty
```

Do not use this helper to replace SDKs or the upstream CLI for normal app code. It exists for local agent workflows, repeatable saved artifacts, and API options the CLI does not expose yet.

Prefer `firecrawl_request.py` for new local-agent scripting because it uses only Python stdlib. Treat older domain workflow scripts as optional examples unless the user specifically asks for those workflows. Model profile flags affect AI-backed formats; plain PDF markdown parsing stays on the local PDF parser path.

## Cross-Agent MCP

Keep Firecrawl tooling separate from any one agent runtime:

- Reusable MCP entrypoint: `scripts/firecrawl-ops/firecrawl_mcp.sh`
- CLI entrypoint: `scripts/firecrawl-ops/firecrawl_cli.sh`
- Direct HTTP helper: `scripts/firecrawl-ops/firecrawl_request.py`
- Direct API: `http://localhost:3002`
- Optional Cursor adapter: `.cursor/mcp.json` plus `.cursor/skills/firecrawl-local-api/SKILL.md`
- Codex/Claude-style adapter: `.agents/skills/firecrawl-local-api/SKILL.md`
- User-level installer: `scripts/firecrawl-ops/sync_agent_skills.sh`

Cursor Composer 2.5 can drive a Cursor SDK agent that calls local Firecrawl through MCP/CLI/API. Use the SDK local runtime for this Mac's Firecrawl stack; cloud agents need a reachable Firecrawl URL. Do not treat Composer as Firecrawl's internal model provider unless Cursor publishes an OpenAI-compatible base URL.

After updating repo skills, run:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

This copies `firecrawl-ops` and `firecrawl-local-api` into `~/.agents/skills` and symlinks them into user-level agent folders.

## Endpoint Selection

Read `references/tools-capabilities.md` when choosing an endpoint. The short version:

- One page: `POST /v2/scrape`
- Search: `POST /v2/search`
- Discover URLs: `POST /v2/map`
- Crawl pages: `POST /v2/crawl` then poll `GET /v2/crawl/:id`
- Batch pages: `POST /v2/batch/scrape` then poll `GET /v2/batch/scrape/:id`
- Local files: `POST /v2/parse` multipart upload
- One-page structured fields: `POST /v2/scrape` with a `json` format
- Multi-page structured fields: `POST /v2/extract` with an explicit schema, then poll `GET /v2/extract/:id`
- Runtime visibility: `GET /v2/team/queue-status`, `GET /v2/crawl/active`

## PDF Parse Notes

For local PDFs, use `POST /v2/parse` or CLI `parse`. Direct HTTP lets you pass parser options that the CLI does not expose:

```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown","html"],"parsers":[{"type":"pdf","mode":"auto","maxPages":25}]}' \
  -F "file=@./report.pdf"
```

Modes:

- `auto`: default. Uses local Rust extraction for simple text PDFs when enabled, then falls back through configured OCR services and finally `pdf-parse`.
- `fast`: avoids OCR-style work; useful for cheap text extraction.
- `ocr`: forces the OCR path when Fire PDF, the local Docling adapter, or MinerU-style services are configured.

Robust layout extraction is not fully local by default. Table-heavy, figure-heavy, scanned, or multi-column PDFs may flatten into markdown; `images` can be empty and `html` can be markdown-derived. For a local, no-Firecrawl-credit OCR/layout backend, start the Docling adapter:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor
scripts/firecrawl-ops/local_firepdf_ocr.sh enable-firecrawl
docker compose up -d --force-recreate api
```

Then parse hard PDFs with:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 --pretty
```

The adapter runs on `127.0.0.1:31337`, Docling Serve runs on `127.0.0.1:5001`, and the API container calls the adapter through `http://host.docker.internal:31337`. The helper pins the known-good Docling Serve CPU image by digest; override `LOCAL_FIREPDF_DOCLING_IMAGE` only for deliberate update testing. Stop it with `scripts/firecrawl-ops/local_firepdf_ocr.sh stop`.

Mode choice matters. On the 2026-05-23 local stress test, a 40-page born-digital spec was best with `fast` because it preserved far more text in 1.7s, while `auto`/`ocr` took about 128s and produced shorter OCR markdown. The 25-page encrypted slide-style CRE report succeeded in all modes, with Docling OCR adding some structure but taking about 46s. Use `fast` first for dense born-digital text PDFs; use `ocr` for scanned/image-only/slide-style files; run the benchmark when unsure.

Useful Docling tuning env vars before `start-adapter` / `start`: `LOCAL_FIREPDF_TIMEOUT_SECONDS` (default 600), `LOCAL_FIREPDF_DOCLING_OCR_PRESET`, `LOCAL_FIREPDF_DOCLING_OCR_LANG`, `LOCAL_FIREPDF_DOCLING_PDF_BACKEND`, `LOCAL_FIREPDF_DOCLING_TABLE_MODE`, `LOCAL_FIREPDF_DOCLING_TO_FORMATS`, and optional enrichment flags. Run `scripts/firecrawl-ops/local_firepdf_ocr.sh settings` to print the full settings surface, then `restart-adapter` to apply changes. Use `scripts/firecrawl-ops/local_firepdf_ocr.sh smoke ./report.pdf` for a one-command OCR parse check. For repeatable comparisons with saved fields and a per-PDF recommended mode:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf \
  --modes fast,auto,ocr --max-pages 40 --out-dir /tmp/firecrawl-pdf-ocr-benchmark --strict
```

## Model Profiles

Use `scripts/firecrawl-ops/set_model_profile.sh <profile>` to rewrite `.env`.

Profiles:

- `budget`: OpenRouter `deepseek/deepseek-v4-flash`; primary cheap model for routine extraction and high-volume discovery. Local profile wiring verified on 2026-05-23.
- `escalated`: OpenRouter `deepseek/deepseek-v4-pro`; smarter fallback for hard extraction, noisy pages, or budget failures.
- `gateway`: Vercel AI Gateway `deepseek/deepseek-v4-flash`; requires a Vercel AI Gateway key.
- `gateway-codex`: Vercel AI Gateway `openai/gpt-5.4-mini`; premium fallback.
- `openai-direct`: OpenAI Platform `gpt-5.4-mini`; requires a Platform `sk-...` key with credits.

After changing profiles:

```bash
docker compose up -d --force-recreate api
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

If `.env` is missing, the script creates a minimal gitignored local env. Add the provider key manually afterward:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
$EDITOR .env
docker compose up -d --force-recreate api
```

## Upstream Sync

Keep fork-owned ops assets in `.agents/`, `docs/firecrawl-ops/`, `scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, and `AGENTS.md`. Sync upstream on a branch, not directly on `main`:

```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

If conflicts appear, prefer upstream for product/API/SDK files and prefer this fork for local ops, skills, model-routing docs, and self-hosted workflow files.

## References And Scripts

The skill folder exposes these via symlinks to `docs/firecrawl-ops/references/` and `scripts/firecrawl-ops/`:

- `references/tools-capabilities.md`: verified local endpoint map and non-working surfaces
- `references/local-pdf-ocr-plan.md`: Docling-first local OCR adapter plan, alternatives, and acceptance criteria
- `references/model-routing.md`: model policy and escalation rules
- `references/ops-playbook.md`: health checks, logs, restart notes
- `references/agent-tooling-firecrawl.md`: reusable MCP/CLI/API setup for Cursor and other agents
- `references/cayman-use-cases-and-playbooks.md`: research/CRE/coding workflows
- `references/cre-access-matrix.md`: source accessibility matrix
- `references/google-flights-scraping.md`: Google Flights scrape pattern
- `references/supabase-schema-firecrawl-swarm.sql`: optional swarm telemetry schema
- `scripts/firecrawl_healthcheck.sh`: local stack smoke test
- `scripts/firecrawl_cli.sh`: Firecrawl CLI wrapper pinned to the local API URL
- `scripts/firecrawl_request.py`: dependency-free direct HTTP helper with output/save controls and advanced parse options
- `scripts/local_firepdf_ocr.sh`: start/stop/health/env/settings/doctor/smoke/benchmark helper for local Docling OCR
- `scripts/local_firepdf_ocr_service.py`: Fire PDF-compatible adapter that lets Firecrawl call local Docling through `/ocr`
- `scripts/pdf_ocr_benchmark.py`: repeatable local PDF parser/OCR matrix runner with saved fields and summaries
- `scripts/firecrawl_mcp.sh`: Firecrawl MCP wrapper pinned to the local API URL
- `scripts/sync_agent_skills.sh`: copy repo skills to `~/.agents/skills` and symlink them into user-level agent folders
- `scripts/set_model_profile.sh`: model profile switcher
- `scripts/sync_upstream_main.sh`: safe upstream merge helper for this fork
- `scripts/artificialanalysis_snapshot.py`: refresh model benchmark data
- `scripts/crawl_swarm.py`, `scripts/firecrawl_swarm_pipeline.py`: batch discovery/scrape workflows
- `scripts/bulk_triage_runner.py`: budget-first triage with escalation batches
- `scripts/platform_access_probe.py`, `scripts/cre_access_matrix.py`: access probes
- `scripts/google_flights_scrape.py`, `scripts/parse_flight_deals.py`: Atlas flight-deal scraper + parser

Load only the specific reference or script needed for the user's task.
