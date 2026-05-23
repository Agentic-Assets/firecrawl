# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

Firecrawl is a web scraper API. The directory you have access to is a monorepo:
- `apps/api` — the API server, queue workers, and scraping engines (TypeScript, the only place most changes land)
- `apps/*-sdk` — language SDKs (js, python, go, java, ruby, rust, php, dot-net, elixir)
- `apps/playwright-service-ts` — headless browser sidecar used by the API
- `apps/go-html-to-md-service` — Go microservice that converts HTML to Markdown
- `apps/nuq-postgres` — Postgres-backed queue (`nuq`) used alongside Redis/RabbitMQ
- `apps/redis`, `apps/test-site`, `apps/test-suite`, `apps/ui` — supporting infra and tests

For local self-hosted setup (Docker compose, env, PowerShell snippets), see `LOCAL_DEVELOPMENT_GUIDE.md` and `SELF_HOST.md`.

## Env files (which is which)

- **`./.env`** — **primary.** This is the file `docker compose up -d` reads at the repo root and is what every local Firecrawl run depends on. Gitignored. Never commit it.
- **`apps/api/.env.example`** — upstream's canonical variable reference. Read this to learn what knobs exist; copy to `./.env` for first-time bootstrap.
- **`apps/api/.env.local`** — tracked upstream artifact with empty values; **not** the file Docker reads despite its `.local` suffix. Ignore unless running `apps/api` directly outside Docker.
- **Fork-specific vars** (`FIRECRAWL_API_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`, optional `OPENROUTER_API_KEY`, `SWARM_SUPABASE_*`) — documented in `LOCAL_DEVELOPMENT_GUIDE.md` §6 and rewritten by `scripts/firecrawl-ops/set_model_profile.sh` where applicable. They live in the root `./.env`.

## Working in `apps/api`

When making changes to the API:

1. Write end-to-end tests that assert your win conditions, if they don't already exist.
   - 1 happy path (more if there are multiple happy paths with significantly different code paths).
   - 1+ failure path(s).
   - E2E (called `snips` in the API) is always preferred over unit testing.
   - Always use `scrapeTimeout` from `./lib` to set the timeout you use for scrapes.
   - Tests run on a variety of configurations. Gate them:
     - Requires fire-engine: `!process.env.TEST_SUITE_SELF_HOSTED`
     - Requires AI: `!process.env.TEST_SUITE_SELF_HOSTED || process.env.OPENAI_API_KEY || process.env.OLLAMA_BASE_URL`
2. Write code to achieve your win conditions.
3. Run tests via `pnpm harness jest <pattern>` from `apps/api`.
   - `pnpm harness` boots the API + workers for the test run. Don't `pnpm start` manually.
   - The full suite is slow — run only the relevant tests locally and let CI cover the rest.
4. Push to a branch, open a PR, let CI verify.

Useful `apps/api` scripts (see `apps/api/package.json` for the full list):
- `pnpm test:snips` — just the snips/E2E suite
- `pnpm dev` — tsx-based dev server via the harness
- `pnpm format`, `pnpm knip` — formatting and dead-code checks

## Self-hosted ops layer (this fork)

This fork adds a self-hosted operations layer on top of upstream Firecrawl. It is fork-only — do not push it upstream.

**Agent skills** (canonical in `.agents/skills/`):
- `firecrawl-ops` — runtime health, Docker, model routing, endpoint selection
- `firecrawl-local-api` — calling the local API at `http://localhost:3002`

Default model routing: budget `deepseek/deepseek-v4-flash`, escalated `deepseek/deepseek-v4-pro` (OpenRouter). Verified locally 2026-05-09.
- `docs/firecrawl-ops/references/` — durable reference docs:
  - `tools-capabilities.md` — endpoint-by-endpoint capability map
  - `model-routing.md` — model strategy and escalation rules
  - `ops-playbook.md` — health checks, debugging, safe ops
  - `cayman-use-cases-and-playbooks.md` — mapped workflows (research/CRE/coding)
  - `cre-access-matrix.md` — CRE platform scrapability matrix (CBRE/Cushman accessible; CoStar/LoopNet blocked)
  - `google-flights-scraping.md` — Atlas travel-deal workflow
  - `supabase-schema-firecrawl-swarm.sql` — optional Supabase schema for swarm telemetry (apply, then set `SWARM_SUPABASE_URL` / `SWARM_SUPABASE_KEY`)
- `scripts/firecrawl-ops/` — runnable ops tooling:
  - `firecrawl_healthcheck.sh` — verify the local stack is up (run this first)
  - `firecrawl_cli.sh` — wrapper for `npx firecrawl-cli` pinned to `http://localhost:3002`
  - `firecrawl_mcp.sh` — wrapper for `npx firecrawl-mcp` pinned to `http://localhost:3002` for any MCP-capable agent
  - `sync_agent_skills.sh` — copy repo Firecrawl skills to `~/.agents/skills` and symlink them into user-level agent folders
  - `set_model_profile.sh budget|escalated|gateway|gateway-codex|openai-direct` — rewrite `.env` model defaults; follow with `docker compose up -d --force-recreate api`
  - `sync_upstream_main.sh` — create an upstream-sync branch, merge `firecrawl/firecrawl:main`, and show protected fork path diffs
  - `artificialanalysis_snapshot.py` — refresh ArtificialAnalysis benchmark data for routing decisions
  - `platform_access_probe.py`, `cre_access_matrix.py` — accessibility probes
  - `bulk_triage_runner.py` — budget-first triage with escalation batches
  - `crawl_swarm.py`, `firecrawl_swarm_pipeline.py` — parallel map+scrape swarm with confidence/provenance output
  - `google_flights_scrape.py`, `parse_flight_deals.py` — Atlas multi-region flight scraper + parser
- Cross-agent integration:
  - `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` — separates the Firecrawl API/CLI/MCP tool layer from Cursor Composer or any other agent model
  - `.cursor/mcp.json` — optional Cursor adapter that registers `firecrawl-local` by calling `scripts/firecrawl-ops/firecrawl_mcp.sh`
  - `.cursor/skills/firecrawl-local-api/SKILL.md` — optional Cursor-native guidance for Composer agents
  - Cursor SDK agents should use local runtime for this Mac's `http://localhost:3002`, pass MCP inline or opt into project settings, and keep Composer 2.5 separate from Firecrawl-internal model routing.
  - `.githooks/post-commit` and `.githooks/pre-push` — advisory reminders to rerun `sync_agent_skills.sh`; enable per clone with `scripts/firecrawl-ops/install_git_hooks.sh`.

When the user asks about scraping workflows, model selection, runtime health, or self-hosted ops, prefer this skill over guessing — invoke it via the Skill tool (`firecrawl-ops`).

## Architecture notes worth knowing up front

- The API is queue-driven. Scrape requests land in `apps/api/src/controllers`, get enqueued (Redis/BullMQ for the legacy path, `nuq` Postgres queue for newer flows), and are picked up by workers under `apps/api/src/services/` (`queue-worker`, `nuq-worker`, `nuq-prefetch-worker`, `nuq-reconciler-worker`, `extract-worker`, `index-worker`).
- Scraping itself lives in `apps/api/src/scraper/scrapeURL/engines/` — multiple engines (fire-engine, playwright, fetch, etc.) selected per request. Tests gated on `TEST_SUITE_SELF_HOSTED` are the ones that need the proprietary fire-engine.
- E2E tests live in `apps/api/src/__tests__/snips/` — these are the canonical "did it work" check.
- HTML→Markdown conversion goes through the Go sidecar (`apps/go-html-to-md-service`), and the browser actions go through `apps/playwright-service-ts`.
