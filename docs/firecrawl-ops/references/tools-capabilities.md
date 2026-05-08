# Firecrawl Docker Tools and Capabilities

## Core endpoints (self-hosted API)

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

## Practical selection guide

- Need one page quickly -> `scrape`
- Need candidate URLs first -> `map` or `search`
- Need many pages -> `crawl`
- Need structured fields/entities -> `extract`

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
