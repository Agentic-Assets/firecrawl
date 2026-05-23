# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Firecrawl is a web scraper API. The directory you have access to is a monorepo:
 - `apps/api` has the actual API and worker code
 - `apps/js-sdk`, `apps/python-sdk`, `apps/rust-sdk` and `apps/java-sdk` are various SDKs

For local development and PowerShell instructions, see `LOCAL_DEVELOPMENT_GUIDE.md`.

When making changes to the API, here are the general steps you should take:
1. Write some end-to-end tests that assert your win conditions, if they don't already exist
  - 1 happy path (more is encouraged if there are multiple happy paths with significantly different code paths taken)
  - 1+ failure path(s)
  - Generally, E2E (called `snips` in the API) is always preferred over unit testing.
  - In the API, always use `scrapeTimeout` from `./lib` to set the timeout you use for scrapes.
  - These tests will be ran on a variety of configurations. You should gate tests in the following manner:
    - If it requires fire-engine: `!process.env.TEST_SUITE_SELF_HOSTED`
    - If it requires AI: `!process.env.TEST_SUITE_SELF_HOSTED || process.env.OPENAI_API_KEY || process.env.OLLAMA_BASE_URL`
2. Write code to achieve your win conditions
3. Run your tests using `pnpm harness jest ...`
  - `pnpm harness` is a command that gets the API server and workers up for you to run the tests. Don't try to `pnpm start` manually.
  - The full test suite takes a long time to run, so you should try to only execute the relevant tests locally, and let CI run the full test suite.
4. Push to a branch, open a PR, and let CI run to verify your win condition.
Keep these steps in mind while building your TODO list.
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
- **Fork-specific vars** (`OPENROUTER_API_KEY`, `MODEL_NAME`, `SWARM_SUPABASE_*`) — documented in `LOCAL_DEVELOPMENT_GUIDE.md` §6 and rewritten by `scripts/firecrawl-ops/set_model_profile.sh`. They live in the root `./.env`.

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

**Agent skills** (canonical in `.agents/skills/`; `.claude/skills/` symlinks there for Claude Code):
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
  - `set_model_profile.sh budget|escalated|gateway|gateway-codex|openai-direct` — rewrite `.env` model defaults; follow with `docker compose up -d --force-recreate api`
  - `artificialanalysis_snapshot.py` — refresh ArtificialAnalysis benchmark data for routing decisions
  - `platform_access_probe.py`, `cre_access_matrix.py` — accessibility probes
  - `bulk_triage_runner.py` — budget-first triage with escalation batches
  - `crawl_swarm.py`, `firecrawl_swarm_pipeline.py` — parallel map+scrape swarm with confidence/provenance output
  - `google_flights_scrape.py`, `parse_flight_deals.py` — Atlas multi-region flight scraper + parser

When the user asks about scraping workflows, model selection, runtime health, or self-hosted ops, prefer this skill over guessing — invoke it via the Skill tool (`firecrawl-ops`).

## Architecture notes worth knowing up front

- The API is queue-driven. Scrape requests land in `apps/api/src/controllers`, get enqueued (Redis/BullMQ for the legacy path, `nuq` Postgres queue for newer flows), and are picked up by workers under `apps/api/src/services/` (`queue-worker`, `nuq-worker`, `nuq-prefetch-worker`, `nuq-reconciler-worker`, `extract-worker`, `index-worker`).
- Scraping itself lives in `apps/api/src/scraper/scrapeURL/engines/` — multiple engines (fire-engine, playwright, fetch, etc.) selected per request. Tests gated on `TEST_SUITE_SELF_HOSTED` are the ones that need the proprietary fire-engine.
- E2E tests live in `apps/api/src/__tests__/snips/` — these are the canonical "did it work" check.
- HTML→Markdown conversion goes through the Go sidecar (`apps/go-html-to-md-service`), and the browser actions go through `apps/playwright-service-ts`.
