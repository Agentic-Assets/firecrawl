# Agent Tooling: Local Firecrawl

This repo keeps the local Firecrawl tool layer separate from any one agent model or IDE.

## Layers

1. **Local Firecrawl runtime**
   - OrbStack + Docker compose stack.
   - API at `http://localhost:3002`.
   - No Firecrawl cloud credits when agents call the local API.

2. **Portable tool interfaces**
   - HTTP API: direct calls to `/v2/scrape`, `/v2/search`, `/v2/map`, `/v2/crawl`, `/v2/batch/scrape`, `/v2/parse`, and `/v2/extract`.
   - CLI: `scripts/firecrawl-ops/firecrawl_cli.sh` runs the upstream Firecrawl CLI against the local API.
   - Agent HTTP helper: `scripts/firecrawl-ops/firecrawl_request.py` for saved artifacts and direct API options the CLI does not expose yet.
   - MCP: `scripts/firecrawl-ops/firecrawl_mcp.sh`.

3. **Agent adapters**
   - Cursor can read `.cursor/mcp.json` and `.cursor/skills/` when configured to use project settings.
   - Other MCP-capable agents can call `scripts/firecrawl-ops/firecrawl_mcp.sh` directly.
   - Codex/Claude-style agents can read `.agents/skills/firecrawl-local-api/SKILL.md`.
   - User-level installs are synced by `scripts/firecrawl-ops/sync_agent_skills.sh`.

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

## CLI And Direct Helper

Use the upstream CLI wrapper first:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o ./out/example.json
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
```

Use the direct helper when an agent needs portable saved artifacts or advanced local API options:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html,images --pdf-mode auto --max-pages 25 \
  --out-dir ./out/firecrawl --save-fields ./out/report-fields --quiet
```

Use official SDKs for application integrations. The helper is intentionally fork-owned local tooling, so upstream app/API/SDK syncs stay simple.

## User-Level Skill Sync

After updating the repo skills, run:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

The script:

- copies `firecrawl-ops` and `firecrawl-local-api` into `~/.agents/skills`
- dereferences repo symlinks so the user-level copies are standalone
- symlinks those canonical copies into `~/.codex/skills`, `~/.claude/skills`, and `~/.cursor/skills`
- skips existing non-symlink destinations unless `--force` is passed

Preview without writing:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh --dry-run
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

## Optional Cursor Adapter

Cursor is just one consumer of the reusable wrapper:

- `.cursor/mcp.json`: registers `firecrawl-local`.
- `.cursor/skills/firecrawl-local-api/SKILL.md`: optional project guidance for Cursor agents.

For the Cursor SDK, do not assume project settings are loaded. Local SDK agents default to no ambient setting sources. Use one of these explicit patterns:

Inline MCP config:

```ts
import { Agent } from "@cursor/sdk";

await Agent.prompt("Use local Firecrawl to scrape https://example.com", {
  apiKey: process.env.CURSOR_API_KEY!,
  model: { id: "composer-2" },
  local: { cwd: process.cwd() },
  mcpServers: {
    "firecrawl-local": {
      type: "stdio",
      command: "bash",
      args: ["scripts/firecrawl-ops/firecrawl_mcp.sh"],
      cwd: process.cwd(),
    },
  },
});
```

Project settings opt-in:

```ts
local: {
  cwd: process.cwd(),
  settingSources: ["project"],
}
```

Use the local SDK runtime for this local Firecrawl stack. Cursor cloud agents run elsewhere, so `http://localhost:3002` means the cloud VM, not this Mac. For cloud agents, use a reachable Firecrawl URL instead of the local wrapper.

## Composer 2.5 Boundary

Use Composer 2.5 to operate the Cursor SDK agent. Let the agent call local Firecrawl through MCP/CLI/API.

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
- "If you need PDF `mode`/`maxPages` or split markdown/html outputs, use `scripts/firecrawl-ops/firecrawl_request.py`."

## Troubleshooting

- MCP tool missing: restart the MCP client after editing its config.
- API unavailable: run `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh`.
- Markdown scrape works but JSON/summary fails: configure model env and recreate the API container.
- Crawl waits forever: submit a crawl, then poll by job id instead of using CLI `--wait`.
