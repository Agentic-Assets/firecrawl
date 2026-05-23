---
name: firecrawl-local-api
description: Use this repo's local Firecrawl API from Cursor or Cursor SDK agents. Use when scraping URLs, searching, mapping/crawling sites, parsing PDFs/Office files, or extracting structured fields with local Firecrawl.
---

# Firecrawl Local API For Cursor

This is an optional Cursor adapter, not the core Firecrawl setup. The reusable Firecrawl layer lives in:

- `scripts/firecrawl-ops/firecrawl_mcp.sh` for MCP
- `scripts/firecrawl-ops/firecrawl_cli.sh` for CLI
- `scripts/firecrawl-ops/firecrawl_request.py` for saved artifacts and advanced direct API options
- `.agents/skills/firecrawl-local-api/SKILL.md` for Codex/Claude-style agents
- `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` for cross-agent setup

## Runtime

- Local API: `http://localhost:3002`
- Local auth: usually disabled with `USE_DB_AUTHENTICATION=false`
- Mac runtime: OrbStack, not Docker Desktop

Start and verify:

```bash
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

## Cursor Tooling

Use the MCP server named `firecrawl-local` first. It is defined in `.cursor/mcp.json` and starts:

```bash
bash scripts/firecrawl-ops/firecrawl_mcp.sh
```

If MCP is unavailable, use the CLI wrapper:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
```

Use `firecrawl_request.py` for direct local API settings the CLI does not expose, especially PDF `mode` and `maxPages`, or when the agent needs split markdown/html/metadata files:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode auto --max-pages 25 --pretty
```

For Cursor SDK code, project settings are not loaded by default. Either pass `mcpServers` inline with `command: "bash"` and `args: ["scripts/firecrawl-ops/firecrawl_mcp.sh"]`, or set `local: { cwd: process.cwd(), settingSources: ["project"] }`.

Use the SDK local runtime for this local stack. Cursor cloud agents cannot reach this Mac's `http://localhost:3002` unless the API is exposed at a reachable URL.

## Endpoint Choices

- One URL: `POST /v2/scrape`
- Search: `POST /v2/search`
- Discover URLs: `POST /v2/map`
- Crawl: `POST /v2/crawl`, then poll `GET /v2/crawl/:id`
- Many URLs: `POST /v2/batch/scrape`, then poll status
- Local files: `POST /v2/parse`
- Structured extraction: `POST /v2/scrape` with JSON format, or `POST /v2/extract` with an explicit schema

Avoid `firecrawl crawl --wait` locally; submit and poll by job id.

## Composer Boundary

Composer 2.5 is the Cursor SDK agent model. Firecrawl is the local web/file tool.

Do not configure Cursor Composer as Firecrawl's internal LLM backend unless Cursor provides an OpenAI-compatible base URL. For Firecrawl's own AI-backed formats, use:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/set_model_profile.sh gateway
docker compose up -d --force-recreate api
```

## Cost Boundary

Local API calls do not spend Firecrawl cloud credits. Provider keys configured in root `.env`, proxies, and hosted search integrations can still have their own costs.
