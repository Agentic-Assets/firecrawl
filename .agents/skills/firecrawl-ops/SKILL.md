---
name: firecrawl-ops
description: Operate, verify, sync, and troubleshoot the self-hosted Firecrawl stack in this fork. Use when the user asks about OrbStack/Docker compose runtime health, local API startup/rebuilds/logs, Firecrawl CLI local setup, upstream sync from firecrawl/firecrawl:main, endpoint capability checks, model routing, OpenRouter/Vercel AI Gateway/OpenAI profile changes, or which Firecrawl methods work locally.
---

# Firecrawl Ops

Use this skill for runtime, sync, and platform work around this fork's self-hosted Firecrawl stack. For directly calling scrape/search/parse endpoints, pair it with `firecrawl-local-api`.

## First Checks

Run from the repo root unless `FC_DIR` is set:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
docker compose ps
```

For durable evidence during reviews or handoffs:

```bash
scripts/firecrawl-ops/firecrawl_healthcheck.sh --evidence-dir tasks/tmp/firecrawl-healthcheck
scripts/firecrawl-ops/local_api_smoke_matrix.py
scripts/firecrawl-ops/local_capability_matrix.py
scripts/firecrawl-ops/pdf_parse_canary.py
scripts/firecrawl-ops/check_pnpm_docker_config.py
```

## CRE Supervised Refresh

`scripts/firecrawl-ops/cre_collector/START_HERE.md` is the canonical CRE
operator guide. For a high-volume listing refresh, do not use raw
`collect.ts --source=all` or `cre_daily_update.sh` as the normal path.

1. Apply `set_cre_resource_profile.sh apply --with-pids`, review `show`, then
   recreate `api` and `playwright-service`.
2. Run `cre_checkpoint_series.py --sources all` with serial collection,
   explicit CPU settings, and `nice`; use the exact command in `START_HERE.md`.
3. The checkpoint guard stops and checkpoints a sustained host-CPU breach. It
   is not a permanent monitor or auto-resume service. Inspect
   `host-cpu-guard.jsonl` and, when present, `cpu-incidents.jsonl` before
   resuming exit `75`.

The supervised path never passes `--monitor`, `--mark-missing`,
`--activate-status`, or `--update-baseline`. GetCREdata remains the only
production OM-extraction writer.

This Mac uses OrbStack, not Docker Desktop. If Docker commands fail, open OrbStack and confirm `docker context show` is `orbstack`. The expected local API is `http://localhost:3002`.

For a fresh clone on another Mac, start with `LOCAL_DEVELOPMENT_GUIDE.md` or `references/partner-orbstack-onboarding.md`. The short path is: install/start OrbStack, confirm `docker context show`, start and verify the core stack without a root `.env`, then have a human create the minimal root `.env` described in the local guide if AI-backed calls are needed. Obtain an operator dry-run plan for the selected model, optionally run `install_git_hooks.sh` and `sync_agent_skills.sh`, then complete the human-owned transition.

## Current Local Reality

Core stack verified after syncing `firecrawl/firecrawl:main` and rebuilding with OrbStack. Local Docling OCR guardrails were added on 2026-05-24, and fresh-clone onboarding was reviewed on 2026-05-25:

- Core stack works: `api`, `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres`.
- NuQ Postgres must have schema table `nuq.queue_scrape`; compose now waits for it via healthcheck.
- Local auth is disabled when `USE_DB_AUTHENTICATION=false`; no bearer token is required.
- Root `.env` is gitignored and may not exist. Non-AI scrape/map/search/parse work without it; AI-backed summary/json/query/extract need provider env.
- The local Firecrawl CLI path works through `scripts/firecrawl-ops/firecrawl_cli.sh`; its normal pin is the checked-in compatibility-manifest value (`firecrawl-cli@1.20.0`).
- The upstream CLI is the default for broad command coverage. Use `scripts/firecrawl-ops/firecrawl_request.py` only when an agent needs dependency-free direct HTTP, advanced `/v2/parse` PDF parser options, or split saved artifacts such as markdown/html/metadata files.
- User-level installed helper scripts also work from other repos at `~/.agents/skills/firecrawl-ops/scripts/`. Set `FC_DIR=/path/to/firecrawl` if the repo is not in the usual local checkout path.
- The CLI `crawl --wait` can hang locally even after the API finishes. For agent automation, use `firecrawl_request.py crawl --wait`, which polls the v2 status endpoint with an explicit bound.
- PDF Rust extraction is enabled by default through compose when `PDF_RUST_EXTRACT_ENABLE` is unset. This improves simple text-based PDFs locally but does not turn scanned/table-heavy PDFs into full layout-aware output.

Model routing:

- `set_model_profile.sh` is retired and always refuses. The CLI, MCP wrapper, request helper, and swarm must not switch a model, rewrite `.env`, or recreate Docker.
- Agent surfaces may request only a dry-run plan, for example `scripts/firecrawl-ops/firecrawl_operator_handoff.py model --profile gateway`. It performs bounded loopback idleness checks and writes a body-free receipt, but does not apply a transition.
- A human operator may apply only after reviewing that plan and supplying the exact attestation required by `firecrawl_operator_handoff.py`; agents must never pass `--apply`.
- For OpenRouter and Vercel AI Gateway profiles, put the provider key in `OPENAI_API_KEY`; these profiles use OpenAI-compatible base URLs.
- `OPENROUTER_API_KEY` exists in API config but is not the default path for these local profiles.
- Use model IDs exactly as provider IDs, without an extra `openrouter/` prefix.

Known local gaps:

- `POST /v2/browser` and `/v2/browser/:sessionId/execute` are registered but need `BROWSER_SERVICE_URL`.
- `POST /v2/agent` is registered but needs `EXTRACT_V3_BETA_URL`.
- Scrape `actions`, screenshot formats, and scrape-browser interaction need Fire Engine or browser-service support.
- AI-backed parse/scrape summary and JSON fail until `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL_NAME` are valid.

## Local CLI

Prefer the wrapper so agents do not forget the local API URL. It is still the upstream Firecrawl CLI, just pinned to the local API:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "firecrawl docs" --limit 3 --json
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o ./out/example.json
# Configuration-changing wrapper flags are intentionally rejected. Ask for a
# dry-run handoff plan before relying on AI-backed formats.
scripts/firecrawl-ops/firecrawl_operator_handoff.py model --profile gateway
```

From another repo, use the installed copy:

```bash
~/.agents/skills/firecrawl-ops/scripts/firecrawl_cli.sh parse ./report.pdf --json --pretty
FC_DIR=/Users/caymanseagraves/Github/agentic-assets/firecrawl ~/.agents/skills/firecrawl-ops/scripts/firecrawl_healthcheck.sh
```

The body-free static compatibility diagnostic does not resolve packages or call the host:

```bash
python3 scripts/firecrawl-ops/firecrawl_compatibility_doctor.py
```

`--run` is an explicit loopback-only operator check for a bounded CLI map call and newline-delimited MCP initialize/tools-list. `@latest` is accepted only by the explicitly acknowledged HUMAN-ONLY upgrade probe and never as an agent default. The CLI wrapper preserves the caller's current directory, so relative upload paths work for commands such as `parse ./report.pdf`. The CLI supports `scrape`, `crawl`, `map`, `parse`, `search`, `agent`, `interact`, `monitor`, setup/config commands, and output flags. For local crawl jobs, use the helper's bounded HTTP poll:

```bash
scripts/firecrawl-ops/firecrawl_request.py crawl https://example.com \
  --limit 1 --scrape-formats markdown,links --wait --metrics-only
```

## Agent HTTP Helper

Use `scripts/firecrawl-ops/firecrawl_request.py` when the upstream CLI is too high-level for an agent task. It uses only Python stdlib, reads `FIRECRAWL_API_URL` / `FIRECRAWL_API_KEY`, preserves caller paths, and supports `--out`, `--out-dir`, and `--save-fields`:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com \
  --formats markdown,links --pretty --out ./out/example.json \
  --save-fields ./out/example-fields --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html,images --pdf-mode auto --max-pages 25 \
  --out-dir ./out/firecrawl --save-fields ./out/report-fields --pretty --quiet

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown --query "What is this document about?" --pretty

scripts/firecrawl-ops/firecrawl_request.py health --metrics-only
```

Do not use this helper to replace SDKs or the upstream CLI for normal app code. It exists for local agent workflows, repeatable saved artifacts, bounded crawl polling, and API options the CLI does not expose yet. Use `--metrics-only` for logs that must not retain source bodies; `--unwrap` deliberately writes only the API response's `data` object.

Prefer `firecrawl_request.py` for new local-agent scripting because it uses only Python stdlib. Treat older domain workflow scripts as optional examples unless the user specifically asks for those workflows. Model profile flags are intentionally unavailable to agent-facing helpers; plain PDF markdown parsing stays on the local PDF parser path.

## Cross-Agent MCP

Keep Firecrawl tooling separate from any one agent runtime:

- Reusable MCP entrypoint: `scripts/firecrawl-ops/firecrawl_mcp.sh`
- CLI entrypoint: `scripts/firecrawl-ops/firecrawl_cli.sh`
- Direct HTTP helper: `scripts/firecrawl-ops/firecrawl_request.py`
- Direct API: `http://localhost:3002`
- Optional Cursor adapter: `.cursor/mcp.json` plus `.cursor/skills/firecrawl-local-api/SKILL.md`
- Codex/Claude-style adapter: `.agents/skills/firecrawl-local-api/SKILL.md`
- User-level installer: `scripts/firecrawl-ops/sync_agent_skills.sh`

Cursor Composer 2.5 can drive a Cursor SDK agent that calls local Firecrawl through MCP/CLI/API. Use the SDK local runtime for this Mac's Firecrawl stack; cloud agents need a reachable Firecrawl URL. Do not treat Composer as Firecrawl's internal model provider unless Cursor publishes an OpenAI-compatible base URL.

After updating repo skills, run:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

This copies `firecrawl-ops` and `firecrawl-local-api` into `~/.agents/skills` and symlinks them into user-level agent folders.

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

For a JS/news hub, map first and then scrape the selected articles. For RSS or Atom, use native HTTP plus an XML feed parser rather than generic scrape. Search is URL discovery only: local selection is Fire Engine when configured, then SearxNG when configured, then DuckDuckGo HTML; do not treat changing hit rank as source evidence. These generic routing rules never replace the CRE collector's source-specific API and SDK contracts.

## PDF Parse Notes

For local PDFs, use `POST /v2/parse` or CLI `parse`. Direct HTTP lets you pass parser options that the CLI does not expose:

```bash
curl -sS -X POST http://localhost:3002/v2/parse \
  -F 'options={"formats":["markdown","html"],"parsers":[{"type":"pdf","mode":"auto","maxPages":25}]}' \
  -F "file=@./report.pdf"
```

Modes:

- `auto`: default. Uses local Rust extraction for simple text PDFs when enabled, then falls back through configured OCR services and finally `pdf-parse`.
- `fast`: avoids OCR-style work; useful for cheap text extraction.
- `ocr`: forces the OCR path when Fire PDF, the local Docling adapter, or MinerU-style services are configured.

Robust layout extraction is not fully enabled on the default parser path. Table-heavy, figure-heavy, scanned, or multi-column PDFs may flatten into markdown; `images` can be empty and `html` can be markdown-derived. For a local, no-Firecrawl-credit OCR/layout backend, an agent may request this guarded dry-run adapter plan:

```bash
# Agent-safe planning only. Never add --apply from an agent surface.
scripts/firecrawl-ops/firecrawl_operator_handoff.py \
  ocr-adapter --profile research-page-aware

# Human operator only, after reviewing a matching plan and approval record.
# Replace AGENTIC-0000 with the actual approved record.
scripts/firecrawl-ops/firecrawl_operator_handoff.py \
  --apply --operator cayman --approval-ref AGENTIC-0000 \
  --approve-provider-cost --confirm "APPLY ocr-adapter research-page-aware" \
  --retain --handoff-ref AGENTIC-0000 \
  --retain-confirm "RETAIN ocr-adapter research-page-aware" \
  ocr-adapter --profile research-page-aware

scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor
```

Then parse hard PDFs with:

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 10 --pretty
```

The adapter runs on `127.0.0.1:31337`, Docling Serve runs on `127.0.0.1:5001`, and the API container calls the adapter through `http://host.docker.internal:31337`. The guarded lifecycle pins the known-good Docling Serve CPU image by digest and fixed loopback binding. A human may plan `ocr-lifecycle --action ensure|restart|stop`; an apply uses the same reviewed human-attestation contract. Agents must not use legacy start/restart/stop aliases to attempt a change.

Mode choice matters. On the 2026-05-23 local stress test, a 40-page born-digital spec was best with `fast` because it preserved far more text in 1.7s, while `auto`/`ocr` took about 128s and produced shorter OCR markdown. The 25-page encrypted slide-style CRE report succeeded in all modes, with Docling OCR adding some structure but taking about 46s. Earlier scanned/image research tests succeeded with `research-page-aware` page markers, but later paper batches exposed low-quality publisher-boilerplate cases. Use `fast` first for dense born-digital text PDFs; use `ocr` for scanned/image-only/slide-style files; run the benchmark when unsure and trust 422 quality failures or QA reports over blanket success claims.

Named profiles live in `scripts/firecrawl-ops/pdf_ocr_profiles.json`. Use `scripts/firecrawl-ops/local_firepdf_ocr.sh profiles` to list them. `research-page-aware`, `tables-accurate`, and `scanned-english` may be proposed through a dry-run `ocr-adapter` handoff. `qa-debug` and raw Docling JSON capture are deliberately unavailable through agent-facing helpers and lifecycle aliases; do not pass capture/output flags or create raw-document artifacts from an agent workflow.

The local adapter now enforces OCR capacity and quality gates. Default `LOCAL_FIREPDF_MAX_CONCURRENT_OCR=2` means a third simultaneous OCR call gets explicit backpressure instead of piling onto Docling; Firecrawl maps that to `SCRAPE_PDF_OCR_BACKPRESSURE` / HTTP 429. Docling timeouts map to `SCRAPE_PDF_OCR_TIMEOUT` / HTTP 504. Low-quality OCR loops, such as one publisher/license page plus mostly empty pages, are rejected by default with `SCRAPE_PDF_LOW_QUALITY` / HTTP 422. Successful parses may expose stable `data.metadata.pdfOcr` metadata: adapter/profile/settings fingerprint, resolved Docling options, page-boundary source, compact per-page quality summaries, boilerplate families/scores, table/figure JSON signals, and low-quality gate settings. Set `LOCAL_FIREPDF_FAIL_LOW_QUALITY=false` only when deliberately collecting bad-output diagnostics.

OCR-mode FirePDF cache is intentionally bypassed so profile/env changes cannot reuse stale OCR. For end-to-end readiness checks, use `scripts/firecrawl-ops/local_firepdf_ocr.sh doctor --smoke-pdf ./report.pdf`; it proves Firecrawl API -> adapter -> Docling without changing settings.

`local_firepdf_ocr.sh settings` prints the historical adapter-tuning surface for inspection only. Agents must not export those values or use start/restart aliases to apply them. Use `scripts/firecrawl-ops/local_firepdf_ocr.sh smoke ./report.pdf` for a one-command OCR parse check. For repeatable comparisons with saved fields, page artifacts, QA reports, accept/reject/manual-review guidance, and a per-PDF recommended mode/profile:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate \
  --max-pages 40 \
  --out-dir /tmp/firecrawl-pdf-ocr-benchmark \
  --strict
```

## Model Profiles

`set_model_profile.sh` is retired. Agents may make a dry-run plan with `scripts/firecrawl-ops/firecrawl_operator_handoff.py model --profile <profile>` but must not apply it.

Profiles:

- `budget`: OpenRouter `deepseek/deepseek-v4-flash`; primary cheap model for routine extraction and high-volume discovery. Local profile wiring verified on 2026-05-23.
- `escalated`: OpenRouter `deepseek/deepseek-v4-pro`; smarter fallback for hard extraction, noisy pages, or budget failures.
- `gateway`: Vercel AI Gateway `deepseek/deepseek-v4-flash-0731`; default model and requires a Vercel AI Gateway key.
- `gateway-pro`: Vercel AI Gateway `deepseek/deepseek-v4-pro-0813`; stronger option for difficult extraction.
- `gateway-codex`: Vercel AI Gateway `openai/gpt-5.4-mini`; retained for explicit legacy use.
- `openai-direct`: OpenAI Platform `gpt-5.4-mini`; requires a Platform `sk-...` key with credits.

For a human-owned apply after a reviewed dry-run, use the operator handoff with an approved reference. For example (replace `AGENTIC-0000` with the approved record):

```bash
scripts/firecrawl-ops/firecrawl_operator_handoff.py \
  --apply --operator cayman --approval-ref AGENTIC-0000 \
  --approve-provider-cost --confirm "APPLY model gateway" \
  --retain --handoff-ref AGENTIC-0000 \
  --retain-confirm "RETAIN model gateway" \
  model --profile gateway
```

If `.env` is missing, create the minimal root file described in `LOCAL_DEVELOPMENT_GUIDE.md` through the normal human-owned setup before requesting a reversible handoff plan. Do not use `apps/api/.env.example` as a Compose contract. Add provider keys only through the approved secret workflow.

## Upstream Sync

Keep fork-owned ops assets in `.agents/`, `docs/firecrawl-ops/`, `scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, and `AGENTS.md`. Sync upstream on a branch, not directly on `main`:

```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

If conflicts appear, prefer upstream for product/API/SDK files and prefer this fork for local ops, skills, model-routing docs, and self-hosted workflow files.

## References And Scripts

The skill folder exposes these via symlinks to `docs/firecrawl-ops/references/` and `scripts/firecrawl-ops/`:

- `references/tools-capabilities.md`: verified local endpoint map and non-working surfaces
- `references/local-pdf-ocr-plan.md`: Docling-first local OCR adapter plan, alternatives, and acceptance criteria
- `references/local-pdf-ocr-research-agent-plan.md`: profile/page-break/raw-JSON/QA plan for research-paper OCR agents
- `references/model-routing.md`: model policy and escalation rules
- `references/ops-playbook.md`: health checks, logs, restart notes
- `references/agent-tooling-firecrawl.md`: reusable MCP/CLI/API setup for Cursor and other agents
- `references/partner-orbstack-onboarding.md`: fresh-clone setup checklist for another Mac
- `references/cayman-use-cases-and-playbooks.md`: research/CRE/coding workflows
- `references/cre-access-matrix.md`: source accessibility matrix
- `references/google-flights-scraping.md`: Google Flights scrape pattern
- `references/supabase-schema-firecrawl-swarm.sql`: optional swarm telemetry schema
- `references/local-capability-matrix.md`: generated v2 route capability matrix from routes, docs, and latest smoke evidence
- `scripts/firecrawl_healthcheck.sh`: local stack smoke test; `--evidence-dir` writes JSON/Markdown proof
- `scripts/local_api_smoke_matrix.py`: core local API route smoke matrix with JSON/Markdown artifacts
- `scripts/local_capability_matrix.py`: regenerate the local v2 capability matrix
- `scripts/pdf_parse_canary.py`: fast/auto PDF parse canaries, with opt-in OCR mode
- `scripts/check_pnpm_docker_config.py`: CI-safe pnpm/Docker native dependency guard
- `scripts/firecrawl_cli.sh`: Firecrawl CLI wrapper pinned to the local API URL
- `scripts/firecrawl_request.py`: dependency-free direct HTTP helper with output/save controls and advanced parse options
- `scripts/local_firepdf_ocr.sh`: start/stop/health/env/settings/doctor/smoke/benchmark helper for local Docling OCR
- `scripts/local_firepdf_ocr_service.py`: Fire PDF-compatible adapter that lets Firecrawl call local Docling through `/ocr`
- `scripts/pdf_ocr_profiles.json`: named Docling OCR profiles
- `scripts/pdf_ocr_benchmark.py`: repeatable local PDF parser/OCR matrix runner with saved fields, page artifacts, QA reports, and summaries
- `scripts/firecrawl_mcp.sh`: Firecrawl MCP wrapper pinned to the local API URL
- `scripts/sync_agent_skills.sh`: copy repo skills to `~/.agents/skills` and symlink them into user-level agent folders
- `scripts/set_model_profile.sh`: retired direct writer; use the guarded operator handoff
- `scripts/sync_upstream_main.sh`: safe upstream merge helper for this fork
- `scripts/artificialanalysis_snapshot.py`: refresh model benchmark data
- `scripts/crawl_swarm.py`, `scripts/firecrawl_swarm_pipeline.py`: batch discovery/scrape workflows; `crawl_swarm.py` normalizes v2 `/map` links and expands same-domain hub links by default, while `firecrawl_swarm_pipeline.py` retries weak markdown pages without switching the shared model profile
- `scripts/bulk_triage_runner.py`: budget-first triage with escalation batches
- `scripts/platform_access_probe.py`, `scripts/cre_access_matrix.py`: access probes
- `scripts/google_flights_scrape.py`, `scripts/parse_flight_deals.py`: Atlas flight-deal scraper + parser
- `references/cre-intelligence-system-design.md`: CRE listing ingestion system design, current collector architecture, source matrix, Supabase schema, agent query API (formerly `cre-listing-system-design.md`, now `archive/cre-listing-system-design-2026-06-12.md`)
- `references/cre-monitor-subsystem.md`: observe-only 007 change-tracking layer (monitor run model and gotchas)
- `scripts/cre_collector/`: production multi-source CRE collector, psql ingestor, daily runner, start-here status, handoff log, lessons
- `scripts/cre_pipeline.py`: legacy Python scraper CLI (run-all, run <broker>, status, export, apply-schema)
- `scripts/cre_scrapers/`: legacy Python package for source probes and detail-page enrichment
- `scripts/sql/`: CRE Supabase SQL migrations (cre_listings, cre_brokerages, contacts, documents, scrape tracking, indexes, views)

Load only the specific reference or script needed for the user's task.
