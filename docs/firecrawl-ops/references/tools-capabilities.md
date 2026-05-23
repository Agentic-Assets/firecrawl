# Firecrawl Docker Tools and Capabilities

## Core endpoints (self-hosted API)

Verified locally on 2026-05-08 after rebuilding the Docker stack.

### `POST /v1/scrape`
- Best for: single URL content extraction
- Typical output: markdown, html, metadata
- Use for: targeted page pulls, doc extraction, quick checks

### `POST /v1/map`
- Best for: URL discovery on a domain/section
- Typical output: discovered links
- Use for: inventory before crawl/extract

### `POST /v1/crawl`
- Best for: multi-page crawling jobs
- Typical output: crawl job state + results/status flow
- Use for: site-wide or section-wide ingestion

### `POST /v1/search`
- Best for: web search retrieval into workflow
- Typical output: ranked URL candidates + snippets
- Use for: discovery before scrape/extract

### `POST /v1/extract`
- Best for: schema/structured extraction from one or more URLs
- Typical output: structured JSON against prompt/schema
- Use for: deterministic fields from messy content

### `POST /v2/scrape`
- Best for: current typed scrape surface
- Typical output: markdown, html/rawHtml, links, images, summary, JSON, attributes/query
- Works locally for markdown, links, summary, and JSON formats when the LLM profile is valid
- `actions` and screenshot formats require Fire Engine or browser-service support

### `POST /v2/parse`
- Best for: local file upload parsing
- Typical output: markdown, plus optional summary/JSON formats
- Works locally for HTML upload and supports document formats through the document converter

### `POST /v2/extract` + `GET /v2/extract/:id`
- Best for: async structured extraction from URLs
- Works locally with an explicit schema and a valid LLM profile
- Prefer v2 scrape `json` format for one-page extraction when possible

### `POST /v2/crawl/params-preview`
- Best for: converting a natural-language crawl instruction into crawl options
- LLM-backed; works locally with a valid LLM profile

### `GET /v2/team/queue-status` and `GET /v2/crawl/active`
- Best for: lightweight local runtime visibility
- Work with auth disabled

## Present but not configured locally

- `POST /v2/browser`, `GET /v2/browser`, `POST /v2/browser/:sessionId/execute` need `BROWSER_SERVICE_URL`.
- `POST /v2/agent` needs `EXTRACT_V3_BETA_URL`.
- `POST /v1/deep-research` starts locally, but it is slower and may keep processing for several minutes.

## Upstream-only (not verified self-hosted)

- `POST /v2/monitor` and related monitor endpoints — page-change monitoring with optional LLM judgment; requires cloud billing and judge configuration. Not part of the local ops stack yet.

## Practical selection guide

- Need one page quickly -> `v2/scrape`
- Need candidate URLs first -> `v2/map` or `v2/search`
- Need many pages -> `v2/crawl`
- Need a local file parsed -> `v2/parse`
- Need structured fields/entities from one page -> `v2/scrape` with `json`
- Need async structured fields/entities from multiple pages -> `v2/extract`

## Runtime stack (docker compose)

Typical services:
- `api`
- `playwright-service`
- `redis`
- `rabbitmq`
- `nuq-postgres`

Health check baseline:
- API reachable at `http://localhost:3002/`
- smoke scrape returns `success: true`
