# Partner OrbStack Onboarding

Use this checklist when setting up this fork on a new Mac.

## What This Repo Runs

This is the Agentic Assets Firecrawl fork. It keeps upstream Firecrawl product code plus a fork-only local operations layer:

- local API on `http://localhost:3002`
- OrbStack/Docker Compose runtime
- local CLI, direct HTTP helper, and MCP wrappers for agents
- optional local Docling OCR adapter for harder PDFs
- model-profile helpers for OpenRouter, Vercel AI Gateway, or OpenAI-compatible providers

## Prerequisites

- OrbStack installed and running.
- Git access to `Agentic-Assets/firecrawl`. If your SSH config uses a host alias, adjust the clone URL accordingly.
- `python3`, `curl`, and `bash`.
- Optional: Node.js/npm for `npx firecrawl-cli`, and `jq` for readable JSON checks.

## Fresh Clone Setup

```bash
git clone git@github.com:Agentic-Assets/firecrawl.git
cd firecrawl

docker context show
docker compose version

scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/install_git_hooks.sh
scripts/firecrawl-ops/sync_agent_skills.sh

docker compose build
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Expected:

- `docker context show` is `orbstack`.
- `http://localhost:3002/` returns the Firecrawl API message.
- `docker compose ps` shows `api`, `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres`.

If Docker is not using OrbStack:

```bash
docker context use orbstack
```

## Local Env

The root `.env` is the only local env Docker Compose reads. It is gitignored and should never be committed.

`scripts/firecrawl-ops/set_model_profile.sh budget` creates a minimal `.env` if one does not exist. Add `OPENAI_API_KEY` only if you need AI-backed calls such as summary, JSON extraction, query, or `/v2/extract`.

Plain scrape, crawl, map, search, and basic PDF parsing can be tested without model-provider spend.

## Daily Commands

```bash
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
docker compose logs api --tail 200
docker compose up -d --force-recreate api
docker compose down
```

Use the local API:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com --formats markdown,links --pretty
```

## Optional Local OCR

Only start OCR when you need scanned/image-heavy PDF handling.

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh enable-firecrawl
docker compose up -d --force-recreate api
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor --smoke-pdf apps/test-site/public/example.pdf
```

Then parse:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 --pretty
```

OCR runs locally through Docling and does not spend Firecrawl cloud credits. External model providers can still cost money when using AI-backed formats.

## After Pulling Changes

```bash
git pull
scripts/firecrawl-ops/sync_agent_skills.sh
docker compose build
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

If only docs or scripts changed, a full Docker rebuild may not be needed. If API TypeScript, Dockerfile, package files, or native code changed, rebuild.

## Upstream Sync

When syncing from `firecrawl/firecrawl:main`, use a branch and preserve fork-owned ops files:

```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

Prefer upstream for product/API/SDK/security files. Prefer this fork for `.agents/`, `docs/firecrawl-ops/`, `scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, `AGENTS.md`, model routing, local OCR, and self-hosted workflow docs.
