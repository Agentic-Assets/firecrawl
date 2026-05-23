# Firecrawl Local Development & PowerShell Guide

This guide is designed to help you master **Firecrawl** while running it locally. Since you're using **PowerShell** and have it integrated with **OpenRouter**, here is your personal playbook for effective scraping and data extraction.

---

### 1. The Local Architecture
When you run `docker compose up -d`, you're spinning up five essential services:
*   **API (`firecrawl-api`)**: The brain that handles your requests.
*   **Playwright Service**: The browser engine that actually "visits" websites.
*   **Redis**: The "waiting room" for jobs and task queuing.
*   **Postgres**: The database for long-term storage (though we currently have `USE_DB_AUTHENTICATION=false`).
*   **RabbitMQ**: Handles internal communication between components.

---

### 2. Mastering the PowerShell Workflow
Since PowerShell's default `curl` is actually an alias for `Invoke-WebRequest`, it can be tricky with JSON. Always use the **Variable + `Invoke-RestMethod`** pattern for reliability:

#### 🟢 The "Happy Path" Scrape
Use this for simple text or markdown retrieval.
```powershell
$body = @{
    url = "https://example.com"
    formats = @("markdown")
    waitFor = 1000 # Give it 1 second to settle
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:3002/v1/scrape" -Method Post -ContentType "application/json" -Body $body
```

#### 🧠 The "Smart" Extraction (OpenRouter)
Since you've configured OpenRouter, you can use LLMs to turn messy websites into clean data.
```powershell
$body = @{
    url = "https://caymanseagraves.com"
    formats = @("extract")
    extract = @{
        schema = @{
            type = "object"
            properties = @{
                publications = @{ type = "array"; items = @{ type = "string" } }
            }
        }
    }
} | ConvertTo-Json -Depth 10 # Use -Depth 10 for nested objects!

Invoke-RestMethod -Uri "http://localhost:3002/v1/scrape" -Method Post -ContentType "application/json" -Body $body
```

---

### 3. Pro-Tips for Effectiveness

#### ⏱️ Handling Dynamic Content (`waitFor`)
Modern sites (React, Next.js, v0) often load content *after* the initial page load. 
*   **Tip**: If your markdown is empty or says "Loading...", increase `waitFor`.
*   **Safe Default**: Use `waitFor = 5000` for heavy sites.

#### 🔍 Debugging with Docker Logs
If a request fails, the terminal output might not tell you *why*. Check the API logs in real-time:
```powershell
# See the last 50 lines and follow new ones
docker logs -f firecrawl-api-1 --tail 50
```
*Look for:* `🐂 Worker taking job` or `Model: <your-model-name>`. (I've updated the code so it now correctly shows your configured `MODEL_NAME` in the logs instead of a hardcoded default).

#### 📂 Bypassing `.gitignore` for `.env`
If you need to change your keys (like OpenRouter), you might find that Cursor or your IDE "hides" the `.env` file. You can always edit it via PowerShell:
```powershell
# Open it in Notepad
notepad .env
# Or restart docker to apply changes
docker compose down; docker compose up -d
```

---

### 4. When to use "Crawl" vs "Scrape"
*   **Scrape**: Use when you have **one specific URL** and want the content now.
*   **Crawl**: Use when you want to find **every page** on a domain (e.g., `caymanseagraves.com/*`).
    *   *Note*: Crawling locally is resource-intensive. Start with a small `limit` (e.g., `limit = 10`).

---

### 5. Essential Local Commands Reference
| Action | Command |
| :--- | :--- |
| **Start everything** | `docker compose up -d` |
| **Stop everything** | `docker compose down` |
| **Check health** | `docker ps` |
| **Reset Database** | `docker volume rm firecrawl_postgres_data` (Caution!) |
| **View API Logs** | `docker logs firecrawl-api-1` |

---

### 🚀 Your OpenRouter / Gateway Advantage
Because you are running locally but can route AI calls through **OpenRouter**, **Vercel AI Gateway**, or **OpenAI**, you are getting the best of both worlds:
1.  **Privacy**: The actual scraping happens on your machine.
2.  **Intelligence**: You can swap models by changing `OPENAI_BASE_URL` and `MODEL_NAME` in your `.env`.

---

### 6. Fork-Specific Env Vars
This fork's ops layer (`.agents/skills/firecrawl-ops/`, `scripts/firecrawl-ops/`) reads a few extra vars from the repo-root `.env`. The model helper creates a minimal gitignored `.env` if one is missing:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
```

| Var | Purpose | Required |
| :--- | :--- | :--- |
| `FIRECRAWL_API_URL` | Local CLI target (`http://localhost:3002`) | Optional but recommended |
| `OPENAI_API_KEY` | Provider key for OpenRouter, Vercel AI Gateway, or OpenAI-compatible profiles | Yes (for AI features) |
| `OPENAI_BASE_URL` | Provider base URL, rewritten by `scripts/firecrawl-ops/set_model_profile.sh` | Yes (for AI features) |
| `MODEL_NAME` | Default LLM, rewritten by `scripts/firecrawl-ops/set_model_profile.sh` | Yes (for AI features) |
| `OPENROUTER_API_KEY` | Optional direct OpenRouter provider path; not the default local profile route | Optional |
| `SWARM_SUPABASE_URL` | Persistent swarm telemetry | Optional |
| `SWARM_SUPABASE_KEY` | Persistent swarm telemetry | Optional |

Switch model profiles without hand-editing:
```bash
scripts/firecrawl-ops/set_model_profile.sh budget      # DeepSeek V4 Flash (OpenRouter)
scripts/firecrawl-ops/set_model_profile.sh escalated   # DeepSeek V4 Pro (OpenRouter)
scripts/firecrawl-ops/set_model_profile.sh gateway     # DeepSeek V4 Flash (Vercel AI Gateway)
docker compose up -d --force-recreate api
```

### 7. Local Firecrawl CLI
Use the fork wrapper so the CLI always talks to your local API:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
```

For crawl jobs, prefer submit + explicit status polling:

```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

### 8. Agent Tooling: MCP, CLI, Cursor Composer
Keep these layers separate:

1. **Firecrawl local runtime**: OrbStack + Docker compose, API at `http://localhost:3002`.
2. **Reusable tool interfaces**: direct HTTP API, `scripts/firecrawl-ops/firecrawl_cli.sh`, and `scripts/firecrawl-ops/firecrawl_mcp.sh`.
3. **Agent adapters**: `.cursor/mcp.json`, `.cursor/skills/`, `.agents/skills/`, or any other MCP-capable client config.
4. **Agent model runtime**: Cursor Composer 2.5, Codex, Claude, or another model.

For MCP-capable agents, use:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

Cursor is already wired to that reusable wrapper through `.cursor/mcp.json` as `firecrawl-local`. Run Cursor SDK agents from the repo root so they can discover `.cursor/mcp.json` and `.cursor/skills/`.

Use Composer 2.5 as the Cursor agent model to take advantage of Cursor SDK pricing. Local Firecrawl remains the web/file tool. Firecrawl's own AI-backed summary/json/extract calls still use `OPENAI_BASE_URL` and `MODEL_NAME` profiles unless Cursor exposes an OpenAI-compatible endpoint.

See `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` for generic MCP client config and Cursor-specific notes.
