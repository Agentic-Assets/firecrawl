# Firecrawl Docker Tools and Capabilities

## Core endpoints (self-hosted API)

Verified locally on 2026-05-23 after rebuilding the OrbStack Docker stack and testing the CLI wrapper.

### `POST /v2/scrape`
- Best for: current typed scrape surface
- Typical output: markdown, html/rawHtml, links, images, summary, JSON, attributes/query
- Works locally for markdown, links, and metadata without model env
- Summary, JSON, query, and schema extraction need a valid model profile
- `actions` and screenshot formats require Fire Engine or browser-service support

### `POST /v2/parse`
- Best for: local file upload parsing
- Typical output: markdown, HTML, links, images, summary, JSON, metadata
- Works locally for HTML/PDF/DOCX/DOC/ODT/RTF/XLSX/XLS
- PDF markdown parsing verified with a 1-page fixture and a 25-page CRE market report
- Summary and JSON parse formats need valid model env

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

## CLI wrapper

Prefer:
```bash
scripts/firecrawl-ops/firecrawl_cli.sh <command> ...
```

The wrapper runs `npx -y firecrawl-cli@latest --api-url http://localhost:3002`. Verified commands:
- `scrape`
- `parse`
- `map`
- `search`
- `crawl` submit + explicit status polling

Use `FIRECRAWL_CLI_PACKAGE=firecrawl-cli@1.18.0` if future `latest` releases break local behavior.

## Present but not configured locally

- `POST /v2/browser`, `GET /v2/browser`, `POST /v2/browser/:sessionId/execute` need `BROWSER_SERVICE_URL`.
- `POST /v2/agent` needs `EXTRACT_V3_BETA_URL`.
- `POST /v1/deep-research` starts locally, but it is slower and may keep processing for several minutes.
- CLI `agent` and `interact` need the corresponding backend services/model configuration.
- CLI `crawl --wait` may hang locally; submit then poll by job id.

## Upstream-only (not verified self-hosted)

- `POST /v2/monitor` and related monitor endpoints — page-change monitoring with optional LLM judgment; requires cloud billing and judge configuration. Not part of the local ops stack yet.

## Practical selection guide

- Need one page quickly -> `v2/scrape`
- Need candidate URLs first -> `v2/map` or `v2/search`
- Need many pages -> `v2/crawl`, then poll status
- Need a local file parsed -> `v2/parse` or CLI `parse`
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
