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
   - Agent HTTP helper: `scripts/firecrawl-ops/firecrawl_request.py` for bounded crawl polling, body-free metrics, saved artifacts, and direct API options the CLI does not expose.
   - MCP: `scripts/firecrawl-ops/firecrawl_mcp.sh`.

3. **Agent adapters**
   - Cursor can read `.cursor/mcp.json` and `.cursor/skills/` when configured to use project settings.
   - Other MCP-capable agents can call `scripts/firecrawl-ops/firecrawl_mcp.sh` directly.
   - Codex/Claude-style agents can read `.agents/skills/firecrawl-local-api/SKILL.md`.
   - User-level installs are synced by `scripts/firecrawl-ops/sync_agent_skills.sh`.

4. **Agent model runtime**
   - Cursor Composer 2.5 is an agent model choice.
   - It is separate from Firecrawl's internal AI model routing.
   - Firecrawl-internal AI formats still use root `.env` values: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`, and the optional `MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK` for one fallback after missing or schema-invalid structured JSON or summary output.

## Reusable MCP Server

Run from the repo root:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

The wrapper starts the upstream-maintained `firecrawl-mcp@3.24.0` package from the checked-in compatibility manifest with:

- `FIRECRAWL_API_URL=http://localhost:3002`
- `FIRECRAWL_API_KEY=local-dev`

`local-dev` is only a placeholder for the auth-disabled local setup. If local auth is enabled later, set `FIRECRAWL_API_KEY` or `TEST_API_KEY` before launching the wrapper.

Normal CLI and MCP paths use the manifest pins (`firecrawl-cli@1.20.0` and `firecrawl-mcp@3.24.0`) when package override variables are unset. Overrides must be exact semver specs. Diagnose the static contract without package resolution or a host call:

```bash
python3 scripts/firecrawl-ops/firecrawl_compatibility_doctor.py
```

The doctor `--run` form is opt-in, accepts only the local loopback API, disables proxies for its own preflight, and supplies child clients with a merged loopback `NO_PROXY` rule while preserving any package-resolution proxy. It uses a body-free bounded CLI map probe and verifies newline-delimited MCP initialize plus `tools/list`. `@latest` exists only in the explicitly acknowledged HUMAN-ONLY upgrade probe; a successful versioned result is evidence for a reviewed manifest update, not an automatic update.

## CLI And Direct Helper

Use the upstream CLI wrapper first:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o ./out/example.json
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
```

Use the direct helper when an agent needs portable saved artifacts, advanced local API options, or bounded crawl polling:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html,images --pdf-mode auto --max-pages 25 \
  --out-dir ./out/firecrawl --save-fields ./out/report-fields --quiet

scripts/firecrawl-ops/firecrawl_request.py crawl https://example.com \
  --limit 1 --scrape-formats markdown,links --wait --metrics-only
```

Use official SDKs for application integrations. The helper is intentionally fork-owned local tooling, so upstream app/API/SDK syncs stay simple. Its default output preserves the API envelope; use `--unwrap` only for a payload-only shape and `--metrics-only` to keep source bodies out of logs. PDF `--max-pages` must be positive. Bounded crawl polling saves a terminal or timeout record and exits nonzero for failed, cancelled, or timed-out crawls, so shell agents do not mistake those states for success.

### Restricted agent-safe pilot

`firecrawl_request.py --agent-safe` is a deliberately tiny, temporary helper
surface, not a general client or a CRE acquisition interface. It permits only
the exact public `https://example.com/` fixture or the tracked synthetic PDF,
uses loopback HTTP with proxies disabled and redirects rejected, and accepts
only fixed one-page/one-crawl bounds. `crawl-status`, raw output, AI/OCR,
profiles, headers, and arbitrary `post` calls are unavailable in this mode.

Agents never provide prerequisite artifacts. Before each safe POST the helper
itself runs the checked-in, GET-only local preflight, requiring `base_http`
ready and zero queue/active-crawl observations. Its internal compatibility
step verifies the normal manifest-pinned CLI version, performs only a loopback
root GET, and checks MCP JSONL initialize plus `tools/list`; it never invokes
the CLI map probe or `@latest`. The full `firecrawl_compatibility_doctor.py
--run` map probe remains an explicit operator diagnostic, not an automatic
agent-safe prerequisite. The helper directly rechecks both read-only endpoints
after compatibility and immediately before the one recipe POST, failing closed
on a missing, false, malformed, or nonzero observation.

The fixed `tasks/agentic-2279/evidence` directory receives only opaque
body-free metrics and a manifest-last terminal receipt. Rejected input or a
prerequisite failure writes no helper artifact; an allowed request writes one
finite terminal result. Do not use this pilot while the shared CRE queue is
active or as a substitute for the governed CRE collector.

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
      "args": ["scripts/firecrawl-ops/firecrawl_mcp.sh"]
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
        "/Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/firecrawl_mcp.sh"
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

For Firecrawl summary, JSON extraction, query, and prompt-backed extract, the
CLI, direct HTTP helper, and MCP wrapper never change model, OCR, Docker, or
healthcheck state. An operator can first produce a guarded, read-only plan:

```bash
scripts/firecrawl-ops/firecrawl_operator_handoff.py model --profile gateway
```

An apply requires the explicit approvals and confirmations shown by that
command. Put the provider key in `OPENAI_API_KEY`. The `gateway` profile uses
Vercel AI Gateway; `budget` and `escalated` use OpenRouter.

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
