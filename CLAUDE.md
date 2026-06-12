# CLAUDE.md

This file provides guidance to Claude Code when working with this repository. Keep it aligned with `AGENTS.md`; that file is the broader Codex-facing source of truth.

Firecrawl is a web scraper API monorepo:
- `apps/api`  -  API server, queue workers, and scraping engines; most product changes land here
- `apps/*-sdk`  -  language SDKs
- `apps/playwright-service-ts`  -  headless browser sidecar
- `apps/go-html-to-md-service`  -  HTML to Markdown sidecar
- `apps/nuq-postgres`  -  Postgres-backed queue used alongside Redis/RabbitMQ
- `apps/redis`, `apps/test-site`, `apps/test-suite`, `apps/ui`  -  supporting infra and tests

For local self-hosted setup, see `LOCAL_DEVELOPMENT_GUIDE.md`, `SELF_HOST.md`, and the `firecrawl-ops` skill.

## Root hygiene

Keep the repo root limited to durable entrypoints, configs, and top-level
context. Put logs, browser captures, and one-off run outputs under a
task-specific folder or `tasks/tmp/`; put durable reference docs under `docs/`;
and keep workflow or example artifacts beside the relevant script or example.

## Env files

- `./.env`  -  primary local Docker compose env. Gitignored. Never commit it.
- `apps/api/.env.example`  -  upstream canonical variable reference.
- `apps/api/.env.local`  -  tracked upstream artifact with empty values; Docker compose does not read it.
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
- `.agents/skills/firecrawl-ops`  -  runtime health, Docker/OrbStack, model routing, upstream sync, endpoint selection
- `.agents/skills/firecrawl-local-api`  -  local API/CLI usage at `http://localhost:3002`
- `docs/firecrawl-ops/references/`  -  durable ops references
- `scripts/firecrawl-ops/`  -  runnable local tools

Key scripts:
- `firecrawl_healthcheck.sh`
- `firecrawl_cli.sh`
- `firecrawl_request.py`
- `firecrawl_mcp.sh`
- `set_model_profile.sh budget|escalated|gateway|gateway-codex|openai-direct`
- `sync_agent_skills.sh`
- `sync_upstream_main.sh`

## CRE listing intelligence (EQUIRE feed)

`scripts/firecrawl-ops/` also contains a full CRE listing ingestion system that
feeds EQUIRE's deal intelligence platform. See each subdirectory's `CLAUDE.md`.

Key components:
- `scripts/firecrawl-ops/cre_collector/`  -  PRODUCTION multi-source collector + ingestor:
  `collect.ts` (14 sources, sale + lease, full pagination through local Firecrawl),
  `cre_ingest.py` (collector JSON -> `credeals` upserts via psql),
  `cre_daily_update.sh` (daily refresh; use `--no-mark-missing` until every source is clean)
- `scripts/firecrawl-ops/cre_scrapers/`  -  legacy Python scraper package
  for source experiments and detail-page enrichment. Do not treat it as the daily bulk path.
- `scripts/firecrawl-ops/sql/`  -  Supabase migrations for `cre_*` tables
  (target: project `fhqycqubkkrdgzswccwd`; apply via `000_run_all.sql`)
- `scripts/firecrawl-ops/cre_pipeline.py`  -  legacy CLI for the Python scraper package
- `scripts/firecrawl-ops/prometheus/`  -  reference Prometheus/CBRE API collector + 11MB dataset
- `scripts/firecrawl-ops/cbre_scrape.py`  -  original single-broker CBRE page scraper (still valid)
- `docs/firecrawl-ops/references/cre-listing-system-design.md`  -  full architecture doc

Current source status changes quickly. Treat these as the canonical status
entrypoints before quoting coverage or making collector changes:
- `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`

Latest verified all-source run started `2026-06-12T04:04:23Z` and produced
35,510 raw records. Later source-specific runs completed Transwestern, Marcus
& Millichap public sale, Lee & Associates public Buildout coverage, and JLL
Investor Center full sitemap detail path (934 active sale rows, 1,857 sitemap
URLs scanned, live-ingested and source-scoped reconciliation completed
2026-06-12) with live ingest, source-scoped reconciliation, and Supabase
validation. Colliers has partial public SalesTracker investment-sale coverage,
but main Colliers Coveo sale/lease search remains blocked. Marcus public lease
remains unsupported.

CBRE has an internal JSON API (`/listings-api/propertylistings/query`) that bypasses
the need for page scraping  -  see `scripts/firecrawl-ops/prometheus/CLAUDE.md`.
Cloudflare still applies; route through local Firecrawl with `proxy=stealth, rawHtml`.

The current ingestor uses `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from the
EQUIRE `.env.local` file and shells out to `psql`. It does not print the URL.
Older REST/service-key loader docs apply only to the legacy Python scraper path.

Start a new CRE collector session at:
- `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`

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
