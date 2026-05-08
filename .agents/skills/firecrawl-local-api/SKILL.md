---
name: firecrawl-local-api
description: Call the locally running self-hosted Firecrawl API on this machine to scrape URLs, crawl sites, parse PDFs/DOCX/XLSX, search the web, or extract structured data with an LLM. Use this skill any time the user wants page content, site maps, document text, or web search results — even if they don't say "Firecrawl" — because there is a working local instance at http://localhost:3002 that handles all of these without paid API credits. Prefer this over generic fetch/curl-and-parse, over headless-browser code from scratch, and over external paid scraping APIs whenever a Firecrawl endpoint covers the job.
---

# Firecrawl (local, self-hosted) — agent quickstart

A working Firecrawl stack runs on this Mac at **`http://localhost:3002`**. This skill tells you how to call it. For runtime/ops/model-routing concerns (Docker health, LLM profile selection, debugging), see the sibling skill at `.claude/skills/firecrawl-ops/SKILL.md` — they cover different layers.

## Auth

Currently **no auth** — `USE_DB_AUTHENTICATION=false` in `.env`, so any caller works. If `TEST_API_KEY` gets set later, send `Authorization: Bearer <TEST_API_KEY>` on every request. Same key for v1 and v2.

## Endpoint cheat-sheet (verified 2026-05-05)

| Endpoint | Purpose |
|---|---|
| `POST /v1/scrape` | Single URL → markdown / html / rawHtml / links / summary / extract. Multi-format works. |
| `POST /v1/map` | Discover URLs on a site. |
| `POST /v1/crawl` + `GET /v1/crawl/:id` | Multi-page crawl, async, poll the GET for status & results. |
| `POST /v1/batch/scrape` + `GET /v1/batch/scrape/:id` | Scrape N URLs in one async job. |
| `POST /v1/extract` | LLM extract from one or more URLs. **Returns the result inline in the POST response — do not poll the GET endpoint.** |
| `POST /v1/search` | Web search, optional `scrapeOptions` to enrich each hit with markdown. Works without SearxNG. |
| `POST /v2/scrape` | v1/scrape + `parsers` option for fine-grained PDF mode control. |
| `POST /v2/parse` | **Multipart file upload** — PDF, DOCX, DOC, ODT, RTF, XLSX, XLS. No URL hosting trick required. |

## PDF / document parsing

Use `/v2/parse` for local files, or `/v2/scrape` with `parsers: [{type:"pdf", mode: "..."}]` for URLs.

PDF parser `mode`:
- `auto` (default) — fast text extraction first, falls back to OCR for scanned pages.
- `fast` — text only; errors out on image-based PDFs.
- `ocr` — force OCR on every page.

OCR is built in. **No LlamaParse key needed.** DOCX / XLSX / etc. are also built-in — no external service.

## Endpoints that don't work locally

These require the proprietary `fire-engine` runtime, which is not part of self-hosted:

- `screenshot`, `screenshot@fullPage` formats
- `actions` (click / wait / type / scroll sequences)

If you need either, use Playwright directly (the `playwright-service` is already up on the internal Docker network) or run a local Playwright script — don't try to make Firecrawl do it.

## LLM-backed features

`extract`, `json`, `summary` formats and `/v1/extract` go through an OpenAI-compatible API set in `.env`:

- `OPENAI_API_KEY` — currently a Vercel AI Gateway key (`vck_…`)
- `OPENAI_BASE_URL` — `https://ai-gateway.vercel.sh/v1`
- `MODEL_NAME` — `deepseek/deepseek-v4-flash` (verified end-to-end with structured outputs)

Switching models is a one-liner: `bash scripts/firecrawl-ops/set_model_profile.sh <profile>`. See the firecrawl-ops skill for profile choices.

## Examples

### curl

**Plain markdown:**
```bash
curl -sS -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","formats":["markdown"]}'
```

**Web search + per-result markdown:**
```bash
curl -sS -X POST http://localhost:3002/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query":"firecrawl docs","limit":3,"scrapeOptions":{"formats":["markdown"]}}'
```

**LLM extract (returns synchronously):**
```bash
curl -sS -X POST http://localhost:3002/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://docs.firecrawl.dev/introduction"],
       "prompt":"Extract page title and one-sentence purpose."}'
```

**Crawl, then poll:**
```bash
ID=$(curl -sS -X POST http://localhost:3002/v1/crawl \
       -H "Content-Type: application/json" \
       -d '{"url":"https://example.com","limit":10,"scrapeOptions":{"formats":["markdown"]}}' \
     | jq -r .id)
curl -sS http://localhost:3002/v1/crawl/$ID | jq '{status, completed, total}'
```

**Local file → markdown via `/v2/parse` (multipart upload):**
```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown"],"parsers":[{"type":"pdf","mode":"auto"}]}' \
  -F "file=@./report.pdf"
```

### Python (stdlib `requests`)

```python
import requests

API = "http://localhost:3002"

# Scrape
r = requests.post(f"{API}/v1/scrape",
                  json={"url": "https://example.com", "formats": ["markdown"]})
md = r.json()["data"]["markdown"]

# Parse a local PDF
with open("report.pdf", "rb") as f:
    r = requests.post(
        f"{API}/v2/parse",
        files={"file": ("report.pdf", f, "application/pdf")},
        data={"options": '{"formats":["markdown"],"parsers":[{"type":"pdf","mode":"auto"}]}'},
    )
md = r.json()["data"]["markdown"]
```

### Node (built-in `fetch`)

```js
const API = "http://localhost:3002";

// Scrape
const r = await fetch(`${API}/v1/scrape`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ url: "https://example.com", formats: ["markdown"] }),
});
const md = (await r.json()).data.markdown;

// Parse a local PDF
const fd = new FormData();
fd.set("options", JSON.stringify({
  formats: ["markdown"],
  parsers: [{ type: "pdf", mode: "auto" }],
}));
fd.set("file", new Blob([await Deno.readFile("report.pdf")], { type: "application/pdf" }),
       "report.pdf");
const p = await fetch(`${API}/v2/parse`, { method: "POST", body: fd });
```

(Node ≥18 native `fetch`/`FormData`; in Node use `fs.openAsBlob` instead of Deno's reader.)

## Choosing the right endpoint

- One URL, want text → `POST /v1/scrape` with `formats: ["markdown"]`.
- Multiple URLs at once → `POST /v1/batch/scrape`.
- Whole site → `POST /v1/map` (just the URL list) or `POST /v1/crawl` (URLs + scraped content).
- Local PDF/DOCX/XLSX → `POST /v2/parse` (multipart).
- Web search → `POST /v1/search`; add `scrapeOptions` to also fetch each result.
- Pull structured data with an LLM → `POST /v1/extract` (one-shot, sync return).

## Where this skill lives

Canonical source: **`<repo>/.agents/skills/firecrawl-local-api/SKILL.md`** — agent-neutral, source-controlled with the stack.

Already symlinked for the agents on this Mac:

| Agent | Path | Resolves to |
|---|---|---|
| Claude Code (project) | `<repo>/.claude/skills/firecrawl-local-api` | canonical |
| Cursor (project) | `<repo>/.cursor/skills/firecrawl-local-api` | canonical |
| User-level registry | `~/.agents/skills/firecrawl-local-api` | canonical |
| Claude Code (user) | `~/.claude/skills/firecrawl-local-api` | user registry |
| Cursor (user) | `~/.cursor/skills/firecrawl-local-api` | user registry |

Pattern: **`~/.agents/skills/`** is the user-level neutral registry. Per-agent folders (`~/.claude/skills/`, `~/.cursor/skills/`) symlink into it. Edit the canonical file once; all agents see it.

To add another agent, point its skill loader at `~/.agents/skills/firecrawl-local-api/`:

- **Claude Desktop** — symlink or copy into the desktop app's user-skills folder (location varies by version; check the in-app skill manager).
- **OpenClaw** — `ln -s ~/.agents/skills/firecrawl-local-api ~/.openclaw/<profile>/skills/firecrawl-local-api`.
- **Codex CLI / Aider / others** — reference the file path in the agent's system prompt or workspace config. It's plain markdown.

If a tool can't load external files (e.g., a one-shot prompt or a `.cursorrules` file that wants inline rules), paste the body of this `SKILL.md` directly into the agent's prompt context.

The endpoint table and examples above are the only durable contract; everything else (model name, gateway, etc.) lives in `.env` and may change.
