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
```

Use the official SDKs in application code. This helper is for cross-agent local runs and advanced local API settings the CLI does not expose yet.

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
- `FIRE_PDF_BASE_URL`, `FIRE_PDF_API_KEY`, `RUNPOD_MU_API_KEY`, `RUNPOD_MU_POD_ID` — optional external OCR/layout services for harder PDFs; not Firecrawl cloud credits, but provider budget may apply
- `SWARM_SUPABASE_URL`, `SWARM_SUPABASE_KEY` — optional, only if using `firecrawl_swarm_pipeline.py` telemetry

Run `scripts/firecrawl-ops/set_model_profile.sh budget` to create a minimal gitignored `.env` if it is missing, then add the provider key manually and recreate the API container.

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

`auto` is the default, `fast` avoids OCR-style work, and `ocr` only becomes meaningfully stronger when Fire PDF or MinerU-style OCR services are configured. Local free parsing is good for text PDFs, but figures, tables, scans, and complex multi-column layouts can still flatten.

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
