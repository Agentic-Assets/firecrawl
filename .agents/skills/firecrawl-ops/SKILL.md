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
- The CLI `crawl --wait` can hang locally even after the API finishes. Prefer submit then status-poll by job id.

Model routing:

- Use `scripts/firecrawl-ops/set_model_profile.sh <profile>` to write `OPENAI_BASE_URL` and `MODEL_NAME` in root `.env`.
- For OpenRouter and Vercel AI Gateway profiles, put the provider key in `OPENAI_API_KEY`; these profiles use OpenAI-compatible base URLs.
- `OPENROUTER_API_KEY` exists in API config but is not the default path for these local profiles.
- Use model IDs exactly as provider IDs, without an extra `openrouter/` prefix.

Known local gaps:

- `POST /v2/browser` and `/v2/browser/:sessionId/execute` are registered but need `BROWSER_SERVICE_URL`.
- `POST /v2/agent` is registered but needs `EXTRACT_V3_BETA_URL`.
- Scrape `actions`, screenshot formats, and scrape-browser interaction need Fire Engine or browser-service support.
- AI-backed parse/scrape summary and JSON fail until `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL_NAME` are valid.

## Local CLI

Prefer the wrapper so agents do not forget the local API URL:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
```

Equivalent raw form:

```bash
FIRECRAWL_API_URL=http://localhost:3002 npx -y firecrawl-cli@latest scrape https://example.com
```

The CLI supports `scrape`, `crawl`, `map`, `parse`, `search`, `agent`, `interact`, `monitor`, setup/config commands, and output flags. For local crawl jobs, use:

```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

## Cross-Agent MCP

Keep Firecrawl tooling separate from any one agent runtime:

- Reusable MCP entrypoint: `scripts/firecrawl-ops/firecrawl_mcp.sh`
- CLI entrypoint: `scripts/firecrawl-ops/firecrawl_cli.sh`
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

## Model Profiles

Use `scripts/firecrawl-ops/set_model_profile.sh <profile>` to rewrite `.env`.

Profiles:

- `budget`: OpenRouter `deepseek/deepseek-v4-flash`; primary cheap model for routine extraction and high-volume discovery. Verified locally for schema-backed `v2/extract` on 2026-05-09.
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
- `references/model-routing.md`: model policy and escalation rules
- `references/ops-playbook.md`: health checks, logs, restart notes
- `references/agent-tooling-firecrawl.md`: reusable MCP/CLI/API setup for Cursor and other agents
- `references/cayman-use-cases-and-playbooks.md`: research/CRE/coding workflows
- `references/cre-access-matrix.md`: source accessibility matrix
- `references/google-flights-scraping.md`: Google Flights scrape pattern
- `references/supabase-schema-firecrawl-swarm.sql`: optional swarm telemetry schema
- `scripts/firecrawl_healthcheck.sh`: local stack smoke test
- `scripts/firecrawl_cli.sh`: Firecrawl CLI wrapper pinned to the local API URL
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
