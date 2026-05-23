---
name: firecrawl-ops
description: Operate, verify, and troubleshoot the self-hosted Firecrawl Docker stack in this repo or on this Mac. Use when the user asks about Firecrawl runtime health, Docker compose rebuild/restart, local API capabilities, endpoint selection, scraping workflows, model routing, OpenRouter/Vercel AI Gateway profile changes, or which Firecrawl methods work locally.
---

# Firecrawl Ops

Use this skill for runtime and platform work around the local self-hosted Firecrawl stack. For directly calling the API, pair it with `firecrawl-local-api`.

## First Checks

Run from the repo root unless `FC_DIR` is set:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
docker compose ps
```

If Docker is down on this Mac, start Docker Desktop first. The expected local API is `http://localhost:3002`.

## Current Local Reality

Verified on 2026-05-08 after a no-cache Docker rebuild:

- Core stack works: `api`, `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres`.
- NuQ Postgres must have schema table `nuq.queue_scrape`; compose now waits for it via healthcheck.
- Local auth is disabled when `USE_DB_AUTHENTICATION=false`; no bearer token is required.
- Current LLM profile is OpenRouter with `MODEL_NAME=deepseek/deepseek-v4-flash`.
- Use model IDs exactly as provider IDs, without an extra `openrouter/` prefix.

Known local gaps:

- `POST /v2/browser` and `/v2/browser/:sessionId/execute` are registered but need `BROWSER_SERVICE_URL`.
- `POST /v2/agent` is registered but needs `EXTRACT_V3_BETA_URL`.
- Scrape `actions`, screenshot formats, and scrape-browser interaction need Fire Engine or browser-service support.

## Endpoint Selection

Read `references/tools-capabilities.md` when choosing an endpoint. The short version:

- One page: `POST /v2/scrape`
- Search: `POST /v2/search`
- Discover URLs: `POST /v2/map`
- Crawl pages: `POST /v2/crawl` then poll `GET /v2/crawl/:id`
- Batch pages: `POST /v2/batch/scrape` then poll `GET /v2/batch/scrape/:id`
- Local files: `POST /v2/parse` multipart upload
- One-page structured fields: `POST /v2/scrape` with a `json` format
- Multi-page structured fields: `POST /v2/extract` with an explicit schema, then poll `GET /v2/extract/:id`
- Runtime visibility: `GET /v2/team/queue-status`, `GET /v2/crawl/active`

## Model Profiles

Use `scripts/firecrawl-ops/set_model_profile.sh <profile>` to rewrite `.env`.

Profiles:

- `budget`: OpenRouter `deepseek/deepseek-v4-flash`; primary cheap model for routine extraction and high-volume discovery. Verified locally for schema-backed `v2/extract` on 2026-05-09.
- `escalated`: OpenRouter `deepseek/deepseek-v4-pro`; smarter fallback for hard extraction, noisy pages, or budget failures.
- `gateway`: Vercel AI Gateway `deepseek/deepseek-v4-flash`; requires a Vercel AI Gateway key.
- `gateway-codex`: Vercel AI Gateway `openai/gpt-5.4-mini`; premium fallback.
- `openai-direct`: OpenAI Platform `gpt-5.4-mini`; requires a Platform `sk-...` key with credits.

After changing profiles:

```bash
docker compose up -d --force-recreate api
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

## References And Scripts

The skill folder exposes these via symlinks to `docs/firecrawl-ops/references/` and `scripts/firecrawl-ops/`:

- `references/tools-capabilities.md`: verified local endpoint map and non-working surfaces
- `references/model-routing.md`: model policy and escalation rules
- `references/ops-playbook.md`: health checks, logs, restart notes
- `references/cayman-use-cases-and-playbooks.md`: research/CRE/coding workflows
- `references/cre-access-matrix.md`: source accessibility matrix
- `references/google-flights-scraping.md`: Google Flights scrape pattern
- `references/supabase-schema-firecrawl-swarm.sql`: optional swarm telemetry schema
- `scripts/firecrawl_healthcheck.sh`: local stack smoke test
- `scripts/set_model_profile.sh`: model profile switcher
- `scripts/artificialanalysis_snapshot.py`: refresh model benchmark data
- `scripts/crawl_swarm.py`, `scripts/firecrawl_swarm_pipeline.py`: batch discovery/scrape workflows
- `scripts/bulk_triage_runner.py`: budget-first triage with escalation batches
- `scripts/platform_access_probe.py`, `scripts/cre_access_matrix.py`: access probes
- `scripts/google_flights_scrape.py`, `scripts/parse_flight_deals.py`: Atlas flight-deal scraper + parser

Load only the specific reference or script needed for the user's task.
