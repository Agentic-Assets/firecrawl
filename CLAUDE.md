# CLAUDE.md

This file provides guidance to Claude Code when working with this repository. Keep it aligned with `AGENTS.md`; that file is the broader Codex-facing source of truth.

Firecrawl is a web scraper API monorepo:
- `apps/api` — API server, queue workers, and scraping engines; most product changes land here
- `apps/*-sdk` — language SDKs
- `apps/playwright-service-ts` — headless browser sidecar
- `apps/go-html-to-md-service` — HTML to Markdown sidecar
- `apps/nuq-postgres` — Postgres-backed queue used alongside Redis/RabbitMQ
- `apps/redis`, `apps/test-site`, `apps/test-suite`, `apps/ui` — supporting infra and tests

For local self-hosted setup, see `LOCAL_DEVELOPMENT_GUIDE.md`, `SELF_HOST.md`, and the `firecrawl-ops` skill.

## Env files

- `./.env` — primary local Docker compose env. Gitignored. Never commit it.
- `apps/api/.env.example` — upstream canonical variable reference.
- `apps/api/.env.local` — tracked upstream artifact with empty values; Docker compose does not read it.
- Fork-specific vars live in root `./.env`: `FIRECRAWL_API_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`, optional `OPENROUTER_API_KEY`, PDF OCR/routing vars, and optional `SWARM_SUPABASE_*`.

## Working in `apps/api`

When changing API behavior:

1. Add focused E2E/snips tests where practical.
   - Include at least one happy path and one failure path when behavior warrants it.
   - Use `scrapeTimeout` from `./lib` for scrape timeouts.
   - Gate fire-engine-only tests with `!process.env.TEST_SUITE_SELF_HOSTED`.
   - Gate AI tests with `!process.env.TEST_SUITE_SELF_HOSTED || process.env.OPENAI_API_KEY || process.env.OLLAMA_BASE_URL`.
2. Implement the smallest code change that satisfies the test.
3. Run targeted tests from `apps/api` with `pnpm harness jest <pattern>`.
4. Push a branch and let CI cover the broader suite.

Useful scripts:
- `pnpm test:snips`
- `pnpm dev`
- `pnpm format`
- `pnpm knip`

## Self-hosted ops layer

This fork adds local operations assets. Keep them fork-only and out of upstream product/API/SDK code unless explicitly needed.

Canonical locations:
- `.agents/skills/firecrawl-ops` — runtime health, Docker/OrbStack, model routing, upstream sync, endpoint selection
- `.agents/skills/firecrawl-local-api` — local API/CLI usage at `http://localhost:3002`
- `docs/firecrawl-ops/references/` — durable ops references
- `scripts/firecrawl-ops/` — runnable local tools

Key scripts:
- `firecrawl_healthcheck.sh`
- `firecrawl_cli.sh`
- `firecrawl_request.py`
- `firecrawl_mcp.sh`
- `set_model_profile.sh budget|escalated|gateway|gateway-codex|openai-direct`
- `sync_agent_skills.sh`
- `sync_upstream_main.sh`

Verified local baseline on 2026-05-23:
- OrbStack Docker compose stack
- local API at `http://localhost:3002`
- upstream CLI wrapper plus local direct helper
- budget model `deepseek/deepseek-v4-flash`; escalated model `deepseek/deepseek-v4-pro`

When the user asks about local scraping workflows, model selection, runtime health, upstream sync, CLI/MCP setup, or self-hosted ops, use the `firecrawl-ops` skill instead of guessing.

## Architecture notes

- The API is queue-driven. Controllers enqueue scrape/crawl/extract work; workers live under `apps/api/src/services/`.
- Scraping engines live in `apps/api/src/scraper/scrapeURL/engines/`.
- E2E tests live in `apps/api/src/__tests__/snips/`.
- HTML to Markdown conversion goes through `apps/go-html-to-md-service`.
- Browser actions go through `apps/playwright-service-ts`.
