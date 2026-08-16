# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

Firecrawl is a web scraper API. The directory you have access to is a monorepo:
- `apps/api` — the API server, queue workers, and scraping engines (TypeScript, the only place most changes land)
- `apps/*-sdk` — language SDKs (js, python, go, java, ruby, rust, php, dot-net, elixir)
- `apps/playwright-service-ts` — headless browser sidecar used by the API
- `apps/go-html-to-md-service` — Go microservice that converts HTML to Markdown
- `apps/nuq-postgres` — Postgres-backed queue (`nuq`) used alongside Redis/RabbitMQ
- `apps/redis`, `apps/test-site`, `apps/test-suite`, `apps/ui` — supporting infra and tests

For local self-hosted setup, see `LOCAL_DEVELOPMENT_GUIDE.md`, `SELF_HOST.md`, and the `firecrawl-ops` skill.

## Root hygiene

Keep the root reserved for durable entrypoints, configs, and top-level context.
Put logs, browser captures, and one-off run outputs under task-specific folders
or `tasks/tmp/`; durable reference docs under `docs/`; and workflow/example
artifacts beside the relevant script or example.

## Linear tracking

- **Team:** `AGENTIC` (`da8832b3-3dde-416f-be01-98c76a5806c7`)
- **Project:** Firecrawl Ops & Automation (`8e2110d7-5a75-4b67-bae1-2c6e8500552d`, slug `f13a738a83bf`)
- **Repository label:** `Agentic-Assets/firecrawl`

For non-trivial Firecrawl work, first search the project for an existing issue.
Create or update the relevant `AGENTIC` issue, apply the repository label, and
add an evidence comment with branch, commit, verification, production gates,
and rollback status. Do not self-assign, mark an issue Done, or alter routing
labels; the hub workflow owns those transitions.

## Shared CRE data ownership

The governed version-5 contract tracked by
[AGENTIC-1233](https://linear.app/agenticassets/issue/AGENTIC-1233) is merged
into Context Engineering `main` through
[PR #145](https://github.com/Agentic-Assets/Agentic-Assets-Context-Engineering/pull/145).
Resolve it from
`$AA_CONTEXT_ROOT/products/equire/cre-data-object-ownership.yaml`. The merge
establishes object ownership and contract tests; it does not itself authorize
production DDL, cache refresh, scheduling, or deployment. The collector must
preserve the five-column OM-facts identity; it does not own OM extraction
writes, market-data objects, or EQUIRE product views.

## Env files (which is which)

- **`./.env`** — **primary when present.** This is the root file Docker Compose reads. It is gitignored and optional for non-AI local calls. Never commit it.
- **`apps/api/.env.example`** — upstream's canonical variable reference, not a drop-in Docker Compose contract. Do not copy it wholesale to root `.env`.
- **`apps/api/.env.local`** — tracked upstream artifact with empty values; **not** the file Docker reads despite its `.local` suffix. Ignore unless running `apps/api` directly outside Docker.
- **Fork-specific vars** (`FIRECRAWL_API_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`, optional `OPENROUTER_API_KEY`, `PDF_RUST_EXTRACT_ENABLE`, optional local Docling/Fire PDF/RunPod OCR vars, `SWARM_SUPABASE_*`) — documented in `LOCAL_DEVELOPMENT_GUIDE.md`. The retired `set_model_profile.sh` never writes them; only a human-reviewed `firecrawl_operator_handoff.py` transition may change its allowlisted model/OCR keys.

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
3. Run tests via `pnpm harness vitest run <pattern>` from `apps/api` (or `pnpm harness pnpm test:snips` for the full snips suite).
   - `pnpm harness` boots the API + workers for the test run. Don't `pnpm start` manually.
   - The full suite is slow — run only the relevant tests locally and let CI cover the rest.
4. Push to a branch, open a PR, let CI verify.

Useful `apps/api` scripts (see `apps/api/package.json` for the full list):
- `pnpm test:snips` — just the snips/E2E suite
- `pnpm dev` — tsx-based dev server via the harness
- `pnpm format`, `pnpm knip` — formatting and dead-code checks

Never bypass `knip` failures, including with `git commit --no-verify`; correct
the reported unused exports or files before committing.

## Self-hosted ops layer (this fork)

This fork adds a self-hosted operations layer on top of upstream Firecrawl. It is fork-only — do not push it upstream. Keep local ops work out of upstream product, API, and SDK paths unless an explicit requirement makes that change necessary.

**Agent skills** (canonical in `.agents/skills/`):
- `firecrawl-ops` — runtime health, Docker, model routing, endpoint selection
- `firecrawl-local-api` — calling the local API at `http://localhost:3002`

Default model routing: Vercel AI Gateway `gateway` profile with
`deepseek/deepseek-v4-flash-0731` and a one-time
`deepseek/deepseek-v4-pro-0813` fallback only for missing or schema-invalid
structured summary/JSON output. `budget` and `escalated` remain explicit
OpenRouter alternatives; shared profile changes require the operator procedure
in `docs/firecrawl-ops/references/model-routing.md`.
- `docs/firecrawl-ops/references/` — durable reference docs:
  - `tools-capabilities.md` — endpoint-by-endpoint capability map
  - `local-pdf-ocr-plan.md` — chosen local Docling OCR adapter plan and alternatives
  - `local-pdf-ocr-research-agent-plan.md` — profile/page-break/raw-JSON/QA roadmap for research-paper OCR agents
  - `model-routing.md` — model strategy and escalation rules
  - `ops-playbook.md` — health checks, debugging, safe ops
  - `partner-orbstack-onboarding.md` — fresh-clone setup checklist for another Mac/business partner
  - `cayman-use-cases-and-playbooks.md` — mapped workflows (research/CRE/coding)
  - `cre-access-matrix.md` — CRE platform scrapability matrix (CBRE/Cushman accessible; CoStar/LoopNet blocked)
  - `google-flights-scraping.md` — Atlas travel-deal workflow
  - `supabase-schema-firecrawl-swarm.sql` — optional Supabase schema for swarm telemetry (apply, then set `SWARM_SUPABASE_URL` / `SWARM_SUPABASE_KEY`)
- `scripts/firecrawl-ops/` — runnable ops tooling:
  - `firecrawl_healthcheck.sh` — verify the local stack is up (run this first)
  - `firecrawl_cli.sh` — wrapper for `npx firecrawl-cli` pinned to `http://localhost:3002`; preserves caller cwd so local parse file paths work
  - `firecrawl_request.py` — dependency-free direct HTTP helper for local agents when they need output/save controls or advanced `/v2/parse` PDF options not exposed by the CLI
  - `local_firepdf_ocr.sh` — start/stop/health/env/settings/profiles/doctor/smoke helper for the local Docling OCR adapter; includes local Docling profiles, `doctor --smoke-pdf`, 429 OCR backpressure, 504 timeout mapping, 422 low-quality rejection, and stable OCR metadata/fingerprints
  - `local_firepdf_ocr_service.py` — Fire PDF-compatible `/ocr` adapter used by Firecrawl when `FIRE_PDF_BASE_URL=http://host.docker.internal:31337`
  - `pdf_ocr_profiles.json` — named Docling OCR profiles such as `research-page-aware`, `tables-accurate`, and `qa-debug`
  - `pdf_ocr_benchmark.py` — repeatable local PDF parser/OCR matrix runner with preflight checks, page artifacts, QA reports, accept/reject/manual-review guidance, and per-PDF mode/profile recommendations
  - `firecrawl_mcp.sh` — wrapper for `npx firecrawl-mcp` pinned to `http://localhost:3002` for any MCP-capable agent
  - `sync_agent_skills.sh` — copy repo Firecrawl skills to `~/.agents/skills` and symlink them into user-level agent folders
  - `firecrawl_operator_handoff.py` — dry-run-first, human-attested operator handoff for allowlisted model/OCR changes; agents must never pass `--apply`
  - `set_model_profile.sh` — retired direct writer that always refuses; use the guarded operator handoff
  - `sync_upstream_main.sh` — create an upstream-sync branch, merge `firecrawl/firecrawl:main`, and show protected fork path diffs
  - Optional older workflow examples: `artificialanalysis_snapshot.py`, `platform_access_probe.py`, `cre_access_matrix.py`, `bulk_triage_runner.py`, `crawl_swarm.py`, `firecrawl_swarm_pipeline.py`, `google_flights_scrape.py`, `parse_flight_deals.py`. Prefer `firecrawl_request.py` for new local-agent scripting.
- Cross-agent integration:
  - `docs/firecrawl-ops/references/agent-tooling-firecrawl.md` — separates the Firecrawl API/CLI/MCP tool layer from Cursor Composer or any other agent model
  - `.cursor/mcp.json` — optional Cursor adapter that registers `firecrawl-local` by calling `scripts/firecrawl-ops/firecrawl_mcp.sh`
  - `.cursor/skills/firecrawl-local-api/SKILL.md` — optional Cursor-native guidance for Composer agents
  - Cursor SDK agents should use local runtime for this Mac's `http://localhost:3002`, pass MCP inline or opt into project settings, and keep Composer 2.5 separate from Firecrawl-internal model routing.
  - `.githooks/post-commit` and `.githooks/pre-push` — advisory reminders to rerun `sync_agent_skills.sh`; enable per clone with `scripts/firecrawl-ops/install_git_hooks.sh`.

When the user asks about scraping workflows, model selection, runtime health, or self-hosted ops, prefer this skill over guessing — invoke it via the Skill tool (`firecrawl-ops`).

For Python changes under `scripts/firecrawl-ops/`, use the relevant local
Ruff skill, verify changed Python with `py_compile`, and read the
`python-testing-patterns` skill before adding or changing tests. For listing
intelligence work, read `scripts/firecrawl-ops/CLAUDE.md` and then the scoped
`cre_collector/` guidance before acting; that current module contract owns
runtime, scheduler, data-write, status-activation, and soft-delete gates.

## Architecture notes worth knowing up front

- The API is queue-driven. Scrape requests land in `apps/api/src/controllers`, get enqueued (Redis/BullMQ for the legacy path, `nuq` Postgres queue for newer flows), and are picked up by workers under `apps/api/src/services/` (`queue-worker`, `nuq-worker`, `nuq-prefetch-worker`, `nuq-reconciler-worker`, `extract-worker`, `index-worker`).
- Scraping itself lives in `apps/api/src/scraper/scrapeURL/engines/` — multiple engines (fire-engine, playwright, fetch, etc.) selected per request. Tests gated on `TEST_SUITE_SELF_HOSTED` are the ones that need the proprietary fire-engine.
- E2E tests live in `apps/api/src/__tests__/snips/` — these are the canonical "did it work" check.
- HTML→Markdown conversion goes through the Go sidecar (`apps/go-html-to-md-service`), and the browser actions go through `apps/playwright-service-ts`.
