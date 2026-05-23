---
name: firecrawl-local-api
description: Call the local self-hosted Firecrawl API at http://localhost:3002 directly or through the Firecrawl CLI. Use when the user wants page scraping, search, map, crawl, batch scrape, local file parsing including PDF/DOCX/XLSX, structured extraction, CLI examples, endpoint selection, local method testing, or guidance on formats/settings for this fork's local API.
---

# Firecrawl Local API

Use this skill to call the local Firecrawl API directly or through the Firecrawl CLI. Use `firecrawl-ops` first when Docker health, OrbStack, model config, rebuilds, upstream sync, or logs matter.

## Assumptions

- Base URL: `http://localhost:3002`
- Auth: currently disabled with `USE_DB_AUTHENTICATION=false`; do not send a bearer token unless `.env` later sets `TEST_API_KEY`.
- Verification date: 2026-05-23 after upstream sync and OrbStack rebuild.
- Cloud credits are not charged when hitting this local API. `creditsUsed` is local accounting metadata. Third-party costs can still occur for AI providers, proxies, or hosted search integrations.
- Root `.env` may be absent. Non-AI scrape/map/search/parse can still work; AI formats need `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL_NAME`.

## What Works Locally

| Endpoint | Status | Use |
|---|---:|---|
| `POST /v2/scrape` | works | Single URL to markdown/html/rawHtml/links/images/summary/json/attributes/query. |
| `POST /v2/search` | works | Web search; `scrapeOptions` can enrich hits with markdown. |
| `POST /v2/map` | works | URL discovery. |
| `POST /v2/crawl` + `GET /v2/crawl/:id` | works | Async crawl with status polling. CLI `--wait` may hang locally; poll by id. |
| `POST /v2/batch/scrape` + `GET /v2/batch/scrape/:id` | works | Async scrape of many URLs. |
| `POST /v2/parse` | works | Multipart upload for local HTML/PDF/DOCX/DOC/ODT/RTF/XLSX/XLS. PDF markdown verified. |
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
- Summary, JSON extraction, query, params-preview, and other AI formats fail until model env is configured.

## LLM-Backed Calls

The local stack reads root `.env`. Change model profiles with:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/set_model_profile.sh escalated
scripts/firecrawl-ops/set_model_profile.sh gateway
scripts/firecrawl-ops/set_model_profile.sh gateway-codex
scripts/firecrawl-ops/set_model_profile.sh openai-direct
docker compose up -d --force-recreate api
```

For OpenRouter and Vercel AI Gateway, put the provider key in `OPENAI_API_KEY`. The helper sets:

- `OPENAI_BASE_URL=https://openrouter.ai/api/v1`
- `MODEL_NAME=deepseek/deepseek-v4-flash`

Use DeepSeek V4 Flash as the primary low-cost model. Escalate to `deepseek/deepseek-v4-pro` for noisy pages, low-confidence fields, or repeated malformed output. If LLM-backed calls fail, check API logs for provider/model errors before blaming the endpoint.

## CLI Patterns

Prefer the fork wrapper:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
```

The wrapper runs `npx -y firecrawl-cli@latest --api-url http://localhost:3002`. Override with `FIRECRAWL_CLI_PACKAGE=firecrawl-cli@1.18.0` if a future latest release breaks.

## Cross-Agent MCP

The reusable local MCP entrypoint is:

```bash
scripts/firecrawl-ops/firecrawl_mcp.sh
```

It runs the upstream `firecrawl-mcp` package against `FIRECRAWL_API_URL=http://localhost:3002`. MCP-capable agents should use this wrapper rather than duplicating package/env setup in each client config.

Cursor is only one optional adapter:

- `.cursor/mcp.json` registers `firecrawl-local` by calling `scripts/firecrawl-ops/firecrawl_mcp.sh`.
- `.cursor/skills/firecrawl-local-api/SKILL.md` gives Cursor/Composer local API guidance.
- `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` explains the reusable MCP/CLI/API layer separately from Cursor Composer.

Composer 2.5 is an agent runtime/model choice. It is not Firecrawl's internal LLM backend unless Cursor provides an OpenAI-compatible model endpoint. Cursor SDK local agents should pass the MCP server inline or opt into project settings with `local.settingSources`; cloud agents cannot reach this Mac's `localhost:3002` unless Firecrawl is exposed at a reachable URL.

For crawl, avoid `--wait` locally:

```bash
ID=$(scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --pretty | jq -r '.data.jobId')
scripts/firecrawl-ops/firecrawl_cli.sh crawl "$ID" --status --pretty
```

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
  -F 'options={"formats":["markdown"],"parsers":["pdf"]}' \
  -F "file=@./report.pdf"
```

Use `{"type":"summary"}` or `{"type":"json"}` only after model env is configured.

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
