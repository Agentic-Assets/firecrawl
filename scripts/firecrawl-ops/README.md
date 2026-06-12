# Firecrawl Ops Scripts

This folder contains the fork-owned operating layer around the self-hosted
Firecrawl stack and the CRE listing collector used by EQUIRE. It is not an
upstream Firecrawl package. Treat it as Agentic Assets local infrastructure.

Use `CLAUDE.md` for compact agent routing. Use this README when you need a
more detailed operator guide.

## What Lives Here

There are two main workstreams:

1. **CRE listing intelligence.** `cre_collector/` collects public commercial
   listing inventory, stages JSON artifacts, and ingests into the EQUIRE
   Supabase project. It is the production path.
2. **Local Firecrawl operations.** The shell and Python helpers run,
   configure, and test the local Firecrawl stack, the CLI, MCP, model
   routing, and local PDF OCR.

Older exploratory scripts remain because they are useful references, but do
not treat every script here as production.

## Quick Checks

From the repo root:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
docker compose ps
```

The expected local API is:

```text
http://localhost:3002
```

This Mac uses OrbStack. If Docker commands fail, check that OrbStack is open
and that the active Docker context is `orbstack`.

## File Guide

| Path | Use |
|---|---|
| `CLAUDE.md` | Compact routing for agents |
| `README.md` | This operator guide |
| `cre_collector/` | Production CRE listing collector and Supabase ingestor |
| `cre_scrapers/` | Legacy Python probes and detail enrichment package |
| `sql/` | Idempotent `credeals` schema migrations |
| `prometheus/` | Original Prometheus CBRE reference implementation and dataset |
| `tests/` | Local OCR service tests |
| `firecrawl_healthcheck.sh` | Local stack smoke test |
| `firecrawl_cli.sh` | Wrapper around upstream Firecrawl CLI pinned to local API |
| `firecrawl_request.py` | Stdlib HTTP helper for direct API calls, saved fields, and parse options |
| `firecrawl_mcp.sh` | MCP wrapper pinned to local API |
| `set_model_profile.sh` | Writes local model routing values into root `.env` |
| `sync_agent_skills.sh` | Copies repo skills into user-level skill folders |
| `sync_upstream_main.sh` | Creates an upstream-sync branch and merges `firecrawl/firecrawl:main` |
| `install_git_hooks.sh` | Installs advisory local git hooks |
| `local_firepdf_ocr.sh` | Starts, stops, checks, and smokes the local Docling OCR adapter |
| `local_firepdf_ocr_service.py` | Fire PDF compatible `/ocr` adapter |
| `pdf_ocr_benchmark.py` | Runs repeatable PDF parser and OCR comparisons |
| `pdf_ocr_profiles.json` | Named local OCR profile settings |

## CRE Listing Collector

Current production path:

```bash
cd scripts/firecrawl-ops/cre_collector
```

Read these before making claims or changing behavior:

1. `START_HERE.md`
2. `CLAUDE.md`
3. `VALIDATION_2026-06-12.md`
4. `BROKERAGE_STATUS_2026-06-12.md`
5. `../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`

Common commands:

```bash
npm run typecheck

npx tsx collect.ts --source=svn --transaction=both --max-items=6 --out=/tmp/svn-probe.json
python3 cre_ingest.py --in /tmp/svn-probe.json --dry-run

npx tsx collect.ts --source=all --transaction=both --max-items=0 \
  --page-cap=400 --concurrency=3 --out=out/full-run.json

python3 cre_ingest.py --in out/full-run.json
bash cre_daily_update.sh --no-mark-missing
```

Use `--mark-missing` only when every relevant source pass completed cleanly
and staged enough rows for the per-broker guardrails. If any source is blocked
or partial, keep the ingest additive.

Current documented baseline from the 2026-06-12 validation cycle:

- Latest full artifact: `cre_collector/out/full_latest_2026-06-11_230423.json`
- Raw listing records: 35,510
- Staged unique upsert rows: 33,488
- Active Supabase rows after additive carryover: 34,218 (intermediate count
  right after the 04:04 UTC full run; later source-specific completions raised
  the live total to 71,600 active rows as of 2026-06-12T23:26 UTC)
- Full collection time: about 27 minutes at concurrency 3
- `--mark-missing` was skipped because Lee and Associates failed

Recent post-run changes are documented in `cre_collector/START_HERE.md`.
Do not quote source coverage from memory or from `cre_scrapers/config.py`.

## CRE Database Target

Target Supabase project:

```text
fhqycqubkkrdgzswccwd
```

Target schema:

```text
credeals
```

Core objects:

| Object | Purpose |
|---|---|
| `cre_brokerages` | Broker registry and scrape configuration |
| `cre_listings` | Canonical listing rows |
| `cre_listing_contacts` | Broker and agent contacts |
| `cre_listing_documents` | OM, brochure, flyer, and related source URLs |
| `cre_listing_images` | Source image URLs |
| `cre_scrape_jobs` | One row per scrape run |
| `cre_scrape_log` | One row per attempted source URL |
| `v_cre_listings_full` | Listing plus child data as JSON arrays |
| `v_cre_active_for_sale` | Active for-sale listings |
| `v_cre_active_for_lease` | Active for-lease listings |
| `v_cre_market_summary` | Per-market aggregates |
| `search_cre_listings(...)` | Full text search plus filters |

The collector stores source URLs for documents and images. It does not
download source PDFs or image binaries into Supabase storage.

The ingestor reads `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from EQUIRE
environment files and shells out to `psql`. It must never print or persist
credential values.

## Source Coverage Notes

The source matrix changes often. Current status belongs in:

```text
cre_collector/START_HERE.md
cre_collector/BROKERAGE_STATUS_2026-06-12.md
```

High-signal cautions:

- CBRE main inventory is active through an internal JSON API behind
  Cloudflare, using local Firecrawl stealth.
- CBRE Deal Flow is a separate public RCM ListingEngine source folded into
  parent CBRE with prefixed IDs.
- Cushman is complete for its public API feed after full run, live ingest,
  source-scoped reconciliation, and Supabase validation (11,318 active rows).
- Newmark uses Algolia credentials embedded in the public page.
- Marcus and Millichap has public sale API coverage and detail enrichment;
  lease inventory is not proven.
- Avison Young uses a public SharpLaunch feed.
- SVN and Lee use Buildout inventory feeds; sustained paging can trigger HTML
  interstitials.
- Colliers has partial public SalesTracker investment-sale support. The main
  Colliers Coveo sale and lease search remains blocked until a usable public
  path or authorized integration exists.
- Transwestern is complete for its public GET feed after full run, live ingest,
  source-scoped reconciliation, and Supabase validation (2,021 active rows).
- JLL Investor Center is complete for the public sitemap detail path: 934
  active U.S. sale rows live-ingested and reconciled on 2026-06-12. No
  coordinates are available from the Investor detail path (known limitation).

## Local CLI Wrapper

Prefer `firecrawl_cli.sh` so calls always hit the local API:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com \
  --format markdown,links --json --pretty

scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty

scripts/firecrawl-ops/firecrawl_cli.sh search "Dallas office for sale" \
  --limit 5 --json --pretty
```

For crawl jobs, submit first and poll by job ID. Local `crawl --wait` can hang
even after the API finishes.

## Direct HTTP Helper

Use `firecrawl_request.py` when an agent needs direct API control without extra
dependencies, split saved artifacts, or PDF parser options the CLI does not
expose.

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com \
  --formats markdown,links --pretty \
  --out ./out/example.json \
  --save-fields ./out/example-fields

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html \
  --pdf-mode auto \
  --max-pages 25 \
  --out-dir ./out/firecrawl-report \
  --pretty
```

This helper is for local agent workflows and repeatable artifacts. Normal app
code should use the product's SDK or API client.

## Model Routing

Switch profiles with:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/set_model_profile.sh escalated
scripts/firecrawl-ops/set_model_profile.sh gateway
scripts/firecrawl-ops/set_model_profile.sh gateway-codex
scripts/firecrawl-ops/set_model_profile.sh openai-direct
docker compose up -d --force-recreate api
```

The script creates or updates root `.env`, which is gitignored and is what
Docker Compose reads. Add provider keys manually. Never commit keys or local
env files.

Plain scrape, map, search, and parse can work without model keys. AI-backed
summary, JSON extraction, query, and extract flows require valid provider
settings.

## Local PDF OCR

Default parse mode is enough for many born-digital PDFs. For scanned,
image-only, table-heavy, slide-style, or layout-sensitive PDFs, start the
local Docling adapter:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor --smoke-pdf ./report.pdf

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html \
  --pdf-mode ocr \
  --max-pages 10 \
  --pretty

scripts/firecrawl-ops/local_firepdf_ocr.sh stop
```

Mode guidance:

- `fast`: born-digital text PDFs, lowest cost and quickest path.
- `auto`: default Firecrawl routing.
- `ocr`: scanned or image-only PDFs, slide decks, and layout-heavy files.

When quality matters, benchmark:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate \
  --max-pages 40 \
  --out-dir /tmp/firecrawl-pdf-ocr-benchmark \
  --strict
```

## Upstream Sync

Do not sync upstream directly on `main`.

```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

Conflict rule of thumb:

- Prefer upstream for product, API, SDK, and core Firecrawl files.
- Prefer this fork for `.agents/`, `docs/firecrawl-ops/`,
  `scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, and local ops docs.

## Skill Sync

After editing repo skills or the source docs they mirror:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

That copies `firecrawl-ops` and `firecrawl-local-api` into
`~/.agents/skills` and symlinks them into user-level agent folders.

## Safe Editing Rules

- Keep secrets out of commits and logs.
- Keep generated collector artifacts in gitignored `out/` paths.
- Do not stage `node_modules/`, `__pycache__/`, generated SQL, or benchmark
  output.
- Do not claim a broker is complete until collection, ingest, and Supabase
  validation are all documented.
- Do not use legacy `cre_scrapers` status as production source truth.
- Do not open a PR through an API. Cayman opens PRs manually.
