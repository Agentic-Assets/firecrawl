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

## Skills

Refresh the relevant skill before non-trivial work. The custom Firecrawl skills
below are this fork's source of truth for the self-hosted stack; the rest cover
Python and Supabase work that touches `scripts/firecrawl-ops/` and `credeals`.

**Custom Firecrawl skills** (fork-owned, in `.agents/skills/`):
- [`firecrawl-ops`](.agents/skills/firecrawl-ops/SKILL.md)  -  self-hosted stack
  runtime and health, Docker/OrbStack, model routing (`set_model_profile.sh`),
  local PDF OCR (Docling adapter), upstream sync, and endpoint selection. Use it
  for any runtime, model-selection, sync, or self-hosted ops question instead of
  guessing.
- [`firecrawl-local-api`](.agents/skills/firecrawl-local-api/SKILL.md)  -  how to
  call the local API and CLI at `http://localhost:3002` (scrape, search, map,
  crawl, parse). Pair it with `firecrawl-ops` when hitting endpoints directly.

**Python Ruff skills** (`.agents/skills/ruff*`):
Normal edit → [`ruff`](.agents/skills/ruff/SKILL.md): `check --diff` on changed files; `format` only when that tree already uses ruff (skill-local `ruff.toml`).
Config / CI / rules → [`ruff-linting`](.agents/skills/ruff-linting/SKILL.md).
Bulk cleanup → [`ruff-recursive-fix`](.agents/skills/ruff-recursive-fix/SKILL.md): safe → unsafe → manual loop on a folder or repo.
Verify with `py_compile`, not repo-wide ruff alone.
New or changed tests → read plugin **`python-testing-patterns`**; run the matching pytest harness (e.g. `scripts/firecrawl-ops/cre_collector/tests/`).

**Python dev plugin skills** (Cursor `claude-code-workflows/python-development`): use the `python-development` plugin skills as appropriate; discover via the Skill tool.

**Supabase plugin skills** (use for DB, Auth, Edge Functions, migrations, RLS):
`supabase`, `supabase:supabase-postgres-best-practices`.
Client API: [Supabase Python reference](https://supabase.com/docs/reference/python/introduction).

## CRE listing intelligence (EQUIRE feed)

`scripts/firecrawl-ops/` also contains a full CRE listing ingestion system that
feeds EQUIRE's deal intelligence platform. See each subdirectory's `CLAUDE.md`.

Key components:
- `scripts/firecrawl-ops/cre_collector/`  -  PRODUCTION multi-source collector + ingestor:
  `collect.ts` (15 sources, sale + lease, full pagination through local Firecrawl),
  `cre_ingest.py` (collector JSON -> `credeals` upserts via psql),
  `cre_daily_update.sh` (daily refresh; use `--no-mark-missing` until every source is clean)
- `scripts/firecrawl-ops/cre_scrapers/`  -  legacy Python scraper package
  for source experiments and detail-page enrichment. Do not treat it as the daily bulk path.
- `scripts/firecrawl-ops/sql/`  -  Supabase migrations for `cre_*` tables
  (target: project `fhqycqubkkrdgzswccwd`; apply via `000_run_all.sql`)
- `scripts/firecrawl-ops/cre_pipeline.py`  -  legacy CLI for the Python scraper package
- `scripts/firecrawl-ops/prometheus/`  -  reference Prometheus/CBRE API collector + 11MB dataset
- `scripts/firecrawl-ops/cbre_scrape.py`  -  original single-broker CBRE page scraper (still valid)
- `docs/firecrawl-ops/references/cre-intelligence-system-design.md`  -  canonical architecture + go-forward monitoring plan
- `docs/firecrawl-ops/references/cre-equire-consumer-api.md`  -  EQUIRE consumer/API reference (views, SQL, env, quick start)

Current source status changes quickly. Treat these as the canonical
entrypoints before quoting coverage or making collector changes (start a new
CRE session here):
- `scripts/firecrawl-ops/cre_collector/START_HERE.md`  -  new-session runbook, live status matrix, and Next Steps
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`  -  collector/ingestor reference
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`  -  per-source coverage, counts, upgrade order

For live per-source counts, the latest baseline, and per-source collection
methods (including the folded `colliers` / `colliers-main` sources and which
sources stay partial), see `START_HERE.md`, `BROKERAGE_STATUS_2026-06-12.md`,
and `cre_collector/CLAUDE.md`.

### Next steps (CRE)

Canonical go-forward plan: section 14 of
`docs/firecrawl-ops/references/cre-intelligence-system-design.md` (per-source
method audit plus the authorized build sequence in 14.4). Live run status (the
in-flight `colliers-main` full run, the additive change-tracking / monitor build
order) lives in `cre_collector/START_HERE.md` and the dated `HANDOFF_*` docs;
do not restate it here.

CBRE has an internal JSON API (`/listings-api/propertylistings/query`) that bypasses
the need for page scraping  -  see `scripts/firecrawl-ops/prometheus/CLAUDE.md`.
Cloudflare still applies; route through local Firecrawl with `proxy=stealth, rawHtml`.

The current ingestor uses `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from the
EQUIRE `.env.local` file and shells out to `psql`. It does not print the URL.
Older REST/service-key loader docs apply only to the legacy Python scraper path.

Verified local baseline on 2026-05-23:
- OrbStack Docker compose stack
- local API at `http://localhost:3002`
- upstream CLI wrapper plus local direct helper
- budget model `deepseek/deepseek-v4-flash`; escalated model `deepseek/deepseek-v4-pro`

## Architecture notes

- The API is queue-driven. Controllers enqueue scrape/crawl/extract work; workers live under `apps/api/src/services/`.
- Scraping engines live in `apps/api/src/scraper/scrapeURL/engines/`.
- E2E tests live in `apps/api/src/__tests__/snips/`.
- HTML to Markdown conversion goes through `apps/go-html-to-md-service`.
- Browser actions go through `apps/playwright-service-ts`.
