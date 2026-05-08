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

### 🚀 Your OpenRouter Advantage
Because you are running locally but using **OpenRouter**, you are getting the best of both worlds:
1.  **Privacy**: The actual scraping happens on your machine.
2.  **Intelligence**: You can swap models (like Claude 3.5 Sonnet or GPT-4o) just by changing the `MODEL_NAME` in your `.env`, without needing to manage multiple API keys.

---

### 6. Fork-Specific Env Vars
This fork's ops layer (`.claude/skills/firecrawl-ops/`, `scripts/firecrawl-ops/`) reads a few extra vars from the repo-root `.env`. There is no root `.env.example` on purpose — copy upstream's `apps/api/.env.example` to `./.env`, then layer these on top:

| Var | Purpose | Required |
| :--- | :--- | :--- |
| `OPENROUTER_API_KEY` | Model routing via OpenRouter | Yes (for AI features) |
| `MODEL_NAME` | Default LLM (rewritten by `scripts/firecrawl-ops/set_model_profile.sh budget\|escalated`) | Yes |
| `SWARM_SUPABASE_URL` | Persistent swarm telemetry | Optional |
| `SWARM_SUPABASE_KEY` | Persistent swarm telemetry | Optional |

Switch model profiles without hand-editing:
```bash
scripts/firecrawl-ops/set_model_profile.sh budget      # MiniMax M2.5
scripts/firecrawl-ops/set_model_profile.sh escalated   # Kimi K2.5
docker compose down && docker compose up -d
```
