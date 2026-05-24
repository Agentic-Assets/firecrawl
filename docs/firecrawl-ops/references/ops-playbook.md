# Firecrawl Ops Playbook

Verified locally on 2026-05-23 with OrbStack after syncing `firecrawl/firecrawl:main`.

## Start / restart
```bash
cd ~/Documents/GitHub/agentic-assets/firecrawl
docker compose up -d
# after model profile or API env changes
docker compose up -d --force-recreate api
# full restart
docker compose down && docker compose up -d
```

## Health check
```bash
scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

## Logs
```bash
cd ~/Documents/GitHub/agentic-assets/firecrawl
docker compose ps
docker compose logs api --tail 200
docker compose logs playwright-service --tail 200
```

## Local CLI
Use the wrapper so the upstream Firecrawl CLI always targets the self-hosted API:
```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o ./out/example.json
scripts/firecrawl-ops/firecrawl_cli.sh --firecrawl-model-profile budget --firecrawl-healthcheck \
  scrape https://example.com --format summary --json --pretty
```

From another repo, use the installed skill copy:
```bash
~/.agents/skills/firecrawl-local-api/scripts/firecrawl_cli.sh parse ./report.pdf --json --pretty
FC_DIR=/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl ~/.agents/skills/firecrawl-ops/scripts/firecrawl_healthcheck.sh
```

The CLI wrapper keeps the caller's current directory, so relative upload paths are safe.

Equivalent direct form:
```bash
FIRECRAWL_API_URL=http://localhost:3002 npx -y firecrawl-cli@latest scrape https://example.com
```

For local crawl jobs, prefer submit + status polling; `firecrawl crawl --wait` can hang locally even after the API finishes:
```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

## Agent HTTP helper
Use `firecrawl_request.py` when an agent needs direct API payload control, advanced PDF parse options, or saved field artifacts:

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

Use the official SDKs in application code. This helper is for cross-agent local runs and advanced local API settings the CLI does not expose yet. Model-profile flags recreate the local API container and affect AI-backed formats; plain PDF markdown parsing remains local parser work.

Prefer `firecrawl_request.py` for new local-agent scripting. Older domain workflow scripts are kept as optional examples, not the default path.

## Cross-agent MCP
Use the reusable wrapper for MCP-capable agents:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

It starts the upstream `firecrawl-mcp` package with `FIRECRAWL_API_URL=http://localhost:3002`. Cursor is configured as one optional consumer via `.cursor/mcp.json`; other agents can use the same command. Composer 2.5, Claude, Codex, or any other model should sit above this tool layer instead of owning it.

For Cursor SDK specifically, use the local runtime for this local API. Either pass this MCP server inline or opt into project settings with `local.settingSources`. Cursor cloud agents cannot reach this Mac's `localhost:3002` unless the API is exposed at a reachable URL.

See `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` for generic client config and Cursor-specific notes.

## User-level skill sync
After editing `.agents/skills/firecrawl-ops` or `.agents/skills/firecrawl-local-api`, copy them into the user-level canonical folder and refresh per-agent symlinks:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

This copies into `~/.agents/skills` and symlinks into `~/.codex/skills`, `~/.claude/skills`, and `~/.cursor/skills`. Use `--dry-run` to preview.

This repo has advisory git hooks in `.githooks/post-commit` and `.githooks/pre-push`. Enable them in a checkout with:

```bash
scripts/firecrawl-ops/install_git_hooks.sh
```

Run that once after cloning on another computer. Git stores `core.hooksPath` in the local checkout config, so the committed hooks travel with the repo but must be enabled per clone. The hooks only print reminders; they do not run the sync script or block commits/pushes.

## Safe operations
- Do not expose postgres port publicly.
- Keep `USE_DB_AUTHENTICATION=false` unless you explicitly configure teams/api keys.
- Treat model key and provider keys as secrets; never commit `.env` secrets.

## Benchmark refresh
Use ArtificialAnalysis snapshot script when you need current leaderboard guidance:
```bash
python3 scripts/firecrawl-ops/artificialanalysis_snapshot.py
```

## Env vars (fork-specific)
Set in the repo-root `.env` so `docker-compose.yaml` picks them up:
- `FIRECRAWL_API_URL=http://localhost:3002` — convenient for CLI/local agents
- `OPENAI_API_KEY` — provider key for OpenRouter, Vercel AI Gateway, or OpenAI-compatible model calls
- `OPENAI_BASE_URL` — provider base URL, rewritten by `scripts/firecrawl-ops/set_model_profile.sh`
- `MODEL_NAME` — default LLM (rewritten by `scripts/firecrawl-ops/set_model_profile.sh`; default budget profile is `deepseek/deepseek-v4-flash`)
- `OPENROUTER_API_KEY` — optional direct OpenRouter provider path; not the default local profile route
- `PDF_RUST_EXTRACT_ENABLE=true` — local Rust PDF text extraction; no cloud credits
- `PDF_SHADOW_COMPARISON_ENABLE=false`, `MINERU_PERCENT=0`, `FIRE_PDF_PERCENT=10` — local PDF routing defaults
- `FIRE_PDF_BASE_URL`, `FIRE_PDF_API_KEY`, `RUNPOD_MU_API_KEY`, `RUNPOD_MU_POD_ID` — optional OCR/layout services for harder PDFs; local Docling uses `FIRE_PDF_BASE_URL=http://host.docker.internal:31337` with an empty key
- `SWARM_SUPABASE_URL`, `SWARM_SUPABASE_KEY` — optional, only if using `firecrawl_swarm_pipeline.py` telemetry

Run `scripts/firecrawl-ops/set_model_profile.sh budget` to create a minimal gitignored `.env` if it is missing, then add the provider key manually and recreate the API container.

## Local Docling OCR adapter
Use this when scanned/image-only PDFs fail or when `--pdf-mode ocr` needs a local OCR/layout backend:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor
scripts/firecrawl-ops/local_firepdf_ocr.sh enable-firecrawl
docker compose up -d --force-recreate api
scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

This starts Docling Serve on `127.0.0.1:5001` and a Fire PDF-compatible adapter on `127.0.0.1:31337`. The helper pins the known-good Docling Serve CPU image by digest and sets `DOCLING_SERVE_MAX_SYNC_WAIT=900` on new starts; override `LOCAL_FIREPDF_DOCLING_IMAGE` or `LOCAL_FIREPDF_DOCLING_MAX_SYNC_WAIT` only when deliberately testing runtime changes. The API container calls the adapter via `http://host.docker.internal:31337`. Stop both with:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh stop
```

Useful checks:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh status
scripts/firecrawl-ops/local_firepdf_ocr.sh logs
scripts/firecrawl-ops/local_firepdf_ocr.sh settings
scripts/firecrawl-ops/local_firepdf_ocr.sh profiles
scripts/firecrawl-ops/local_firepdf_ocr.sh smoke ./report.pdf
curl -sS http://127.0.0.1:31337/health | jq .
```

Named profiles live in `scripts/firecrawl-ops/pdf_ocr_profiles.json`. Use `default` for conservative OCR, `research-page-aware` for academic page chunks, `tables-accurate` or `tables-fast` for table experiments, `scanned-english` for image-only English scans, `qa-debug` for raw Docling JSON capture, and `figure-enrichment-lab` only for benchmarks. Apply profile changes with:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh restart-adapter --profile tables-accurate
scripts/firecrawl-ops/local_firepdf_ocr.sh restart-adapter --profile qa-debug --capture-json
```

Raw Docling JSON capture is off unless a profile enables it or `--capture-json` is passed. It saves full-document data under `tasks/tmp/firecrawl-docling-debug` by default, so keep it out of commits.

The adapter has guardrails for heavy agent runs. `LOCAL_FIREPDF_MAX_CONCURRENT_OCR=2` by default; excess concurrent requests return `SCRAPE_PDF_OCR_BACKPRESSURE` / HTTP 429. Docling timeouts return `SCRAPE_PDF_OCR_TIMEOUT` / HTTP 504. Low-quality OCR dominated by publisher/license boilerplate or empty pages returns `SCRAPE_PDF_LOW_QUALITY` / HTTP 422 by default. Successful Firecrawl responses may include `data.metadata.pdfOcr` quality metrics.

Useful Docling tuning env vars before `start-adapter` / `start`; explicit env vars override the named profile:

- `LOCAL_FIREPDF_TIMEOUT_SECONDS=600` by default; raise it for very large/image-heavy papers
- `LOCAL_FIREPDF_MAX_CONCURRENT_OCR=2` by default; lower it for fragile local runs or raise it only after benchmarking
- `LOCAL_FIREPDF_FAIL_LOW_QUALITY=true` by default; set false only for diagnostics
- `LOCAL_FIREPDF_DOCLING_MAX_SYNC_WAIT=900` by default on new Docling container starts; this requires starting or recreating Docling Serve, not only restarting the adapter
- `LOCAL_FIREPDF_DOCLING_OCR_PRESET=auto|easyocr|tesseract`
- `LOCAL_FIREPDF_DOCLING_OCR_LANG=en[,de,...]`
- `LOCAL_FIREPDF_DOCLING_PDF_BACKEND=docling_parse|pypdfium2|dlparse_v4`
- `LOCAL_FIREPDF_DOCLING_TABLE_MODE=accurate|fast`
- `LOCAL_FIREPDF_DOCLING_TO_FORMATS=md,json,html`

After changing these env vars, run `scripts/firecrawl-ops/local_firepdf_ocr.sh restart-adapter` so the adapter container picks them up. For direct adapter experiments, `POST /ocr` may include a `docling_options` object; Firecrawl API calls use the process-level adapter profile/env.

## PDF parser behavior
Use direct HTTP when you need PDF parser knobs:
```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown","html"],"parsers":[{"type":"pdf","mode":"auto","maxPages":25}]}' \
  -F "file=@./report.pdf"
```

Equivalent helper form:
```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode auto --max-pages 25 --pretty
```

`auto` is the default, `fast` avoids OCR-style work, and `ocr` only becomes meaningfully stronger when Fire PDF, local Docling, or MinerU-style OCR services are configured. Local free parsing is good for text PDFs, but figures, tables, scans, and complex multi-column layouts can still flatten. Dense born-digital PDFs often do best with `fast`; scanned/image-only/slide-style PDFs are the better OCR candidates. With the Docling adapter enabled, use:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 --pretty
```

Run a repeatable local matrix:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate \
  --max-pages 40 \
  --out-dir /tmp/firecrawl-pdf-ocr-benchmark \
  --strict
```

The benchmark preflights fake `.pdf` downloads, restarts the adapter between OCR profiles unless `--no-profile-restart` is passed, saves split markdown/html/metadata fields, writes `fields/pages.jsonl`, and adds per-case `qa.json` / `qa.md`. The root `summary.md` includes a recommended mode/profile per PDF.

## Upstream sync
Use a branch and merge commit so fork-specific ops assets remain easy to review:
```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

Protected fork areas:
- `.agents/`
- `docs/firecrawl-ops/`
- `scripts/firecrawl-ops/`
- `LOCAL_DEVELOPMENT_GUIDE.md`
- `AGENTS.md`

Prefer upstream for product/API/SDK/security files. Prefer the fork for local ops, skill files, model routing, and self-hosted workflow docs.
