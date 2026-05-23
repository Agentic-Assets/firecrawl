# Agent Tooling: Local Firecrawl

This repo keeps the local Firecrawl tool layer separate from any one agent model or IDE.

## Layers

1. **Local Firecrawl runtime**
   - OrbStack + Docker compose stack.
   - API at `http://localhost:3002`.
   - No Firecrawl cloud credits when agents call the local API.

2. **Portable tool interfaces**
   - HTTP API: direct calls to `/v2/scrape`, `/v2/search`, `/v2/map`, `/v2/crawl`, `/v2/batch/scrape`, `/v2/parse`, and `/v2/extract`.
   - CLI: `scripts/firecrawl-ops/firecrawl_cli.sh`.
   - MCP: `scripts/firecrawl-ops/firecrawl_mcp.sh`.

3. **Agent adapters**
   - Cursor reads `.cursor/mcp.json` and `.cursor/skills/`.
   - Other MCP-capable agents can call `scripts/firecrawl-ops/firecrawl_mcp.sh` directly.
   - Codex/Claude-style agents can read `.agents/skills/firecrawl-local-api/SKILL.md`.

4. **Agent model runtime**
   - Cursor Composer 2.5 is an agent model choice.
   - It is separate from Firecrawl's internal AI model routing.
   - Firecrawl-internal AI formats still use root `.env` values: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL_NAME`.

## Reusable MCP Server

Run from the repo root:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

The wrapper starts the upstream-maintained `firecrawl-mcp` package with:

- `FIRECRAWL_API_URL=http://localhost:3002`
- `FIRECRAWL_API_KEY=local-dev`

`local-dev` is only a placeholder for the auth-disabled local setup. If local auth is enabled later, set `FIRECRAWL_API_KEY` or `TEST_API_KEY` before launching the wrapper.

Override the package version if needed:

```bash
FIRECRAWL_MCP_PACKAGE=firecrawl-mcp@3.17.0 scripts/firecrawl-ops/firecrawl_mcp.sh
```

## Generic MCP Client Config

Use this shape in MCP clients that support stdio command servers:

```json
{
  "mcpServers": {
    "firecrawl-local": {
      "command": "bash",
      "args": [
        "scripts/firecrawl-ops/firecrawl_mcp.sh"
      ]
    }
  }
}
```

If the client does not run from the repo root, use the absolute path:

```json
{
  "mcpServers": {
    "firecrawl-local": {
      "command": "bash",
      "args": [
        "/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/firecrawl_mcp.sh"
      ]
    }
  }
}
```

## Cursor Adapter

Cursor's repo config points at the same reusable wrapper:

- `.cursor/mcp.json`: registers `firecrawl-local`.
- `.cursor/skills/firecrawl-local-api/SKILL.md`: tells Cursor/Composer agents how to use local Firecrawl.

Run Cursor SDK agents from the repo root so they discover both files.

## Composer 2.5 Boundary

Use Composer 2.5 to operate the agent. Let the agent call local Firecrawl through MCP/CLI/API.

Do not set Firecrawl's `OPENAI_BASE_URL` to Cursor unless Cursor provides an OpenAI-compatible endpoint. Cursor SDK model aliases like `composer-latest` belong to the Cursor agent layer, not Firecrawl's internal model provider layer.

## Firecrawl Internal Model Routing

For Firecrawl summary, JSON extraction, query, and prompt-backed extract:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/set_model_profile.sh gateway
docker compose up -d --force-recreate api
```

Put the provider key in `OPENAI_API_KEY`. The `gateway` profile uses Vercel AI Gateway; `budget` and `escalated` use OpenRouter.

## Good Agent Prompts

- "Use the firecrawl-local MCP server to scrape this URL as markdown and links."
- "Use local Firecrawl parse for this PDF path; do not use Firecrawl cloud."
- "Map the site with local Firecrawl first, then batch scrape the most relevant URLs."
- "If MCP is unavailable, use `scripts/firecrawl-ops/firecrawl_cli.sh`."

## Troubleshooting

- MCP tool missing: restart the MCP client after editing its config.
- API unavailable: run `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh`.
- Markdown scrape works but JSON/summary fails: configure model env and recreate the API container.
- Crawl waits forever: submit a crawl, then poll by job id instead of using CLI `--wait`.
