---
name: firecrawl-local-api
description: Call the locally running self-hosted Firecrawl API at http://localhost:3002 for scraping, search, crawl, batch scrape, local file parsing, PDF/DOCX/XLSX conversion, and structured extraction. Use when the user wants page content, site maps, document text, web search results, local Firecrawl method testing, or a decision about which Firecrawl endpoint to use.
---

# Firecrawl Local API

Use this skill to call the local Firecrawl API directly. Use `firecrawl-ops` first when Docker health, model config, rebuilds, or logs matter.

## Assumptions

- Base URL: `http://localhost:3002`
- Auth: currently disabled with `USE_DB_AUTHENTICATION=false`; do not send a bearer token unless `.env` later sets `TEST_API_KEY`.
- Verification date: 2026-05-08 after a local Docker rebuild.

## What Works Locally

| Endpoint | Status | Use |
|---|---:|---|
| `POST /v2/scrape` | works | Single URL to markdown/html/rawHtml/links/images/summary/json/attributes/query. |
| `POST /v2/search` | works | Web search; `scrapeOptions` can enrich hits with markdown. |
| `POST /v2/map` | works | URL discovery. |
| `POST /v2/crawl` + `GET /v2/crawl/:id` | works | Async crawl with status polling. |
| `POST /v2/batch/scrape` + `GET /v2/batch/scrape/:id` | works | Async scrape of many URLs. |
| `POST /v2/parse` | works | Multipart upload for local HTML/PDF/DOCX/DOC/ODT/RTF/XLSX/XLS. |
| `POST /v2/extract` + `GET /v2/extract/:id` | works with schema | Async structured extraction. Provide an explicit schema. |
| `POST /v2/crawl/params-preview` | works | LLM-backed natural-language crawl options. |
| `GET /v2/team/queue-status` | works | Local queue visibility. |
| `GET /v2/crawl/active` | works | Active crawl listing. |
| `POST /v1/extract` | works with schema | Synchronous structured extraction when schema is explicit. |
| `POST /v1/llmstxt` + `GET /v1/llmstxt/:id` | works | Generate llms.txt style output. |

Prefer v2 endpoints for new work unless the user specifically asks for v1.

## Not Configured Locally

- `POST /v2/browser`, `GET /v2/browser`, and `POST /v2/browser/:sessionId/execute`: need `BROWSER_SERVICE_URL`.
- `POST /v2/agent`: needs `EXTRACT_V3_BETA_URL`.
- Scrape `actions`, screenshot formats, and scrape-browser interaction: need Fire Engine or browser-service support.
- Prompt-only extract/schema generation may fail on weaker budget models; provide an explicit schema.

## LLM-Backed Calls

The local stack uses `.env`:

- `OPENAI_BASE_URL=https://openrouter.ai/api/v1`
- `MODEL_NAME=deepseek/deepseek-v4-flash`

Use DeepSeek V4 Flash as the primary low-cost model. It was verified locally for schema-backed `v2/extract` on 2026-05-09. Escalate to `deepseek/deepseek-v4-pro` for noisy pages, low-confidence fields, or repeated malformed output. If LLM-backed calls fail, check API logs for provider/model errors before blaming the endpoint.

## Curl Patterns

Plain scrape:

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","formats":["markdown","links"]}'
```

Structured one-page extraction:

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","formats":["markdown",{"type":"json","prompt":"Extract the domain and page heading.","schema":{"type":"object","properties":{"domain":{"type":"string"},"heading":{"type":"string"}},"required":["domain","heading"]}}]}'
```

Local file parse:

```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown",{"type":"summary"}]}' \
  -F "file=@./report.pdf"
```

Async extract with schema:

```bash
ID=$(curl -sS -X POST http://localhost:3002/v2/extract \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://example.com"],"prompt":"Extract the page heading.","schema":{"type":"object","properties":{"heading":{"type":"string"}},"required":["heading"]},"enableWebSearch":false}' \
  | jq -r .id)
curl -sS "http://localhost:3002/v2/extract/$ID"
```

Search with scraped snippets:

```bash
curl -sS -X POST http://localhost:3002/v2/search \
  -H "Content-Type: application/json" \
  -d '{"query":"firecrawl docs","limit":3,"scrapeOptions":{"formats":["markdown"]}}'
```

## Choosing Quickly

- Need one page text: `v2/scrape` with `formats:["markdown"]`.
- Need fields from one page: `v2/scrape` with a `json` format.
- Need fields from multiple URLs: `v2/extract` with schema.
- Need a PDF or Office file on disk: `v2/parse`.
- Need a site inventory: `v2/map`.
- Need many pages: `v2/crawl` or `v2/batch/scrape`.
- Need current runtime state: use `firecrawl-ops`, then `v2/team/queue-status`.
