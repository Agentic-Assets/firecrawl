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
Use the wrapper so the CLI always targets the self-hosted API:
```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
```

Equivalent direct form:
```bash
FIRECRAWL_API_URL=http://localhost:3002 npx -y firecrawl-cli@latest scrape https://example.com
```

For local crawl jobs, prefer submit + status polling; `firecrawl crawl --wait` can hang locally even after the API finishes:
```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

## Cross-agent MCP
Use the reusable wrapper for MCP-capable agents:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

It starts the upstream `firecrawl-mcp` package with `FIRECRAWL_API_URL=http://localhost:3002`. Cursor is configured as one optional consumer via `.cursor/mcp.json`; other agents can use the same command. Composer 2.5, Claude, Codex, or any other model should sit above this tool layer instead of owning it.

For Cursor SDK specifically, use the local runtime for this local API. Either pass this MCP server inline or opt into project settings with `local.settingSources`. Cursor cloud agents cannot reach this Mac's `localhost:3002` unless the API is exposed at a reachable URL.

See `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` for generic client config and Cursor-specific notes.

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
- `SWARM_SUPABASE_URL`, `SWARM_SUPABASE_KEY` — optional, only if using `firecrawl_swarm_pipeline.py` telemetry

Run `scripts/firecrawl-ops/set_model_profile.sh budget` to create a minimal gitignored `.env` if it is missing, then add the provider key manually and recreate the API container.

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
