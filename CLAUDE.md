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

`scripts/firecrawl-ops/` contains the production CRE listing ingestion system for
EQUIRE. See each subdirectory's `CLAUDE.md` for module detail.

Key components:
- `scripts/firecrawl-ops/cre_collector/` - production collector + ingestor:
  `collect.ts` (CLI entry, 15 sources), `types.ts`, `lib/`, `sources/<broker>.ts`,
  `cre_ingest.py` (full artifact upsert via psql; status activation OPT-IN
  default-off via `--activate-status`/`CRE_ACTIVATE_STATUS=1`), `cre_monitor.py`
  and `cre_gate.py` (observe-only 007 change tracking; gate wired into daily
  script as step [3/4]; monitor enqueues new/changed into `cre_enrichment_queue`),
  `cre_enrich.py` (Tier-B queue worker: drains `cre_enrichment_queue`, runs
  `collect.ts --enrich-input` targeted detail, re-ingests additively; never
  soft-deletes or activates status), `cre_daily_update.sh` (full collect refresh;
  use `--no-mark-missing` while Savills sale is structurally capped),
  `launchd/cre_run_tier.sh` (portable-lock tier dispatcher; writes a
  per-run verdict marker), `cre_status.sh` (read-only run-health heartbeat:
  schedules, staleness, last-run verdict, TCC/stack/env state)
- `scripts/firecrawl-ops/cre_scrapers/` - legacy Python package for source
  experiments and detail enrichment. Not the daily bulk path.
- `scripts/firecrawl-ops/sql/` - Supabase migrations for `cre_*` tables
  (target: project `fhqycqubkkrdgzswccwd`; apply via `000_run_all.sql`)
- `scripts/firecrawl-ops/prometheus/` - reference Prometheus/CBRE API collector
- `docs/firecrawl-ops/references/cre-intelligence-system-design.md` - architecture
- `docs/firecrawl-ops/references/cre-equire-consumer-api.md` - EQUIRE consumer API
- `docs/firecrawl-ops/references/cre-monitor-subsystem.md` - monitor run model
- `docs/firecrawl-ops/references/cre-phase2-board-impact-2026-06-13.md` -
  Phase-2 status activation board impact (gated)
- `docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md` -
  where to run the pipeline (cloud vs Mac mini); split-architecture
  recommendation, platform comparison, anti-bot IP risk (decision aid, not actioned)

Canonical entrypoints (live counts and per-source status only in `START_HERE.md`):
- `scripts/firecrawl-ops/cre_collector/SETUP.md` - fresh-clone setup runbook
  (Mac mini + dev); run `cre_setup.sh` first, then `launchd/install_launchd.sh`
- `scripts/firecrawl-ops/cre_collector/START_HERE.md` - runbook, status matrix
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md` - collector/ingestor reference
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md` - upgrade order
- `scripts/firecrawl-ops/cre_collector/HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` -
  monitor hardening, modular refactor, first `--apply` seed
- `scripts/firecrawl-ops/cre_collector/HANDOFF_COLLIERS_MAIN_2026-06-13.md` -
  colliers-main full detail run (COMPLETE as of 2026-06-14; 15,829 active rows)

### Monitor tracks (2026-06-13/14)

**Track 1 (shipped):** monitor hardening, `collect.ts` modular split, coverage
gate triple-gating, four detail-id monitor exclusions, first gated
`cre_monitor.py --apply` seed on `avison-young`.

**Track 2 (shipped 2026-06-13):** observe-only seed scaled to all 11
monitor-enabled sources (`cre_source_baseline`=11, `cre_source_index`=73,693, 0
events, board unchanged); `jll`/`jll-investor` monitor short-circuit before
enumeration; Phase-2 status activation WIRED + hardened in `cre_ingest.py`
(Choice (a) COALESCE, terminal-stickiness guard, default-off
`CRE_STATUS_FLIP_MAX_FRACTION` circuit breaker); the EQUIRE board-gate widening
(Option B, 6 sites) committed on `dynamically-display-cre-listing-data` branch
`feat/multi-source-live-listings`; the agent-facing `005` views widened to
`status IN ('active','under_contract','pending')` on this branch (apply gated,
verified read-only as a zero-row no-op today). Gate-0 prod status CHECK verified.

**Track 2 (additionally shipped 2026-06-14):** colliers-main full run COMPLETE
and ingested additively (status activation OFF); colliers brokerage total 17,001
active (15,829 main-site rows: 5,750 sale + 8,897 lease + 1,182 sale_or_lease,
0 soft-deleted, 0 duplicate external_ids); live board total 87,328 active (0
non-active, 0 NULL status). Status activation changed to OPT-IN default-off
(requires `--activate-status` or `CRE_ACTIVATE_STATUS=1`; new helpers
`_status_activation_enabled()` and `apply_status_activation_gate()`). 290 pytest
pass. `cre_gate.py` wired into `cre_daily_update.sh` as observe-only step [3/4]
with auto-downgrade fail-safe. Monitor (`ai.agentic.cre-monitor`, every 3h at
:15, `CRE_MONITOR_APPLY=1`) and daily (`ai.agentic.cre-daily`, 06:30 daily,
`--no-mark-missing`, status activation OFF) launchd tiers are LOADED AND
EXECUTING on schedule: the repo was relocated out of `~/Documents` to
`/Users/caymanseagraves/Github/agentic-assets/firecrawl`, so the prior macOS
TCC / Full Disk Access exit-126 block no longer applies. The monitor tier has a
confirmed clean scheduled run (`out/daily/last_run_monitor.json` rc:0,
2026-06-15); a later monitor fire correctly skips when the daily tier holds the
run lock. The daily tier executes on schedule (additive `--no-mark-missing`).
Weekly reconcile tier NOT loaded (held for explicit go-ahead). Live DB
hardening applied
(cap_rate/occupancy_rate CHECKs, FK ON DELETE SET NULL, NULLS NOT DISTINCT
indexes, security_invoker reasserted; no board change). Data-quality cleanups:
50 JLL board-invisible rows set inactive, transwestern config restored, Savills
residential contamination removed (101 mis-categorized sale + 1 ghost lease
soft-deleted; 2 defensible Chicago retail lease rows remain). Savills sale is
structurally capped (no public US commercial-sale feed).

**Still gated for go-ahead:** deploy the consumer board-gate branch (must
precede live T3.1 activation), trigger the first live status activation, and
apply the widened `005` views (live DDL, alongside the consumer deploy). See the
phase2 board-impact doc's activation runbook. The Tier-B `cre_enrichment_queue`
worker (`cre_enrich.py`) and the cadence restructure (monitor 2x/day + enrich
every 4h + additive weekly backstop; daily retired) SHIPPED in code 2026-06-15;
the live launchd cutover (apply `sql/010`, reload monitor at 2x/day, load enrich,
unload daily, load the additive weekly) is held for go-ahead per
`cre_collector/ENRICHMENT_WORKER_DESIGN_2026-06-15.md` Section 9. The
`CRE_WEEKLY_MARK_MISSING=1` soft-delete escalation stays separately gated.

### Next steps (CRE)

Canonical plan: section 14 of `cre-intelligence-system-design.md`. Live run
status (monitor rollout order, next open items) lives in
`cre_collector/START_HERE.md` and dated `HANDOFF_*` docs; do not restate here.

CBRE has an internal JSON API (`/listings-api/propertylistings/query`). Route
through local Firecrawl with `proxy=stealth, rawHtml` (see `prometheus/CLAUDE.md`).

The ingestor uses `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from the EQUIRE
`.env.local` file and shells out to `psql`. It does not print the URL.

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
