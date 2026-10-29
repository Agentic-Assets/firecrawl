READ ~/AGENTS.md FIRST (skip if missing).

# AGENTS.md - cre_collector/

> **ACTIVE production collector and ingest implementation.** The supervised
> refresh and series entrypoints are `cre_checkpoint_refresh.py` and
> `cre_checkpoint_series.py`; the Windows task wrapper and its acceptance gates
> are documented in the [guarded tier guide](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md).
> Scheduler state requires live readback. Do **not** implement daily
> collection in `../cre_scrapers/` (legacy
> Python probes/archives only).

> **Historical runtime source, 2026-07-11:** The Mac mini audit found no CRE
> launchd jobs, markers, or collector artifacts at that point in time. For
> current supervised refresh procedure, use the
> [Strict listing refresh](START_HERE.md#strict-listing-refresh) section and
> obtain current runtime/task/settlement evidence from
> [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752). Current
> source blockers and remaining-source acceptance live on
> [AGENTIC-2902](https://linear.app/agenticassets/issue/AGENTIC-2902). The July 11
> operator runbook is historical scheduler-recovery and OM-canary context; use
> it only when that history or recovery path is in scope. For the target of unattended
> scheduled operation and its unproven acceptance cases, use the
> [operating goal](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md#unattended-operating-goal).

Multi-source CRE listing collector + Supabase ingestor. This is the
**production path** for building and refreshing the `credeals` listing
database (EQUIRE feed). It supersedes the per-broker Python scrapers in
`../cre_scrapers/` for bulk collection (`../cre_scrapers/brokers/*/scraper.py`
is **stale** for production; README/archive there is reference-only).

## Agent routing: where to edit

| Task | Edit here (ACTIVE) | Do NOT use (STALE) |
|------|-------------------|-------------------|
| New/changed broker collect logic | `sources/<broker>.ts`, `collect.ts` | `../cre_scrapers/brokers/*/scraper.py` |
| Ingest / board upsert | `cre_ingest.py` | `../cre_pipeline.py`, `../cre_scrapers/pipeline.py` |
| Supervised refresh / series | `cre_checkpoint_refresh.py`, `cre_checkpoint_series.py`; Windows wrapper in `START_HERE.md` and the guarded tier guide | `cre_daily_update.sh` (legacy), `launchd/` (host-specific/historical), `../cre_scrapers/config.py` |
| Live counts / source status | fresh `cre_validate.py` output + checkpoint manifest | dated docs or `../cre_scrapers/config.py` `active` flags |
| Broker endpoint research notes | read `../cre_scrapers/brokers/*/README.md` | do not treat as runtime code |

Adapted from the Prometheus cloud collector reference in `../prometheus/`.
Uses source-specific public feeds and direct HTTP where configured, and the
local self-hosted Firecrawl API where required. No hosted Firecrawl API key is
required for the collector.

## Live counts and run-health (agents)

No Markdown file is a live-count source. For live operating claims such as
scheduler state or index size, use current runtime/scheduler observations and
artifact readback. For listing counts, use a current `cre_validate.py` result or
generation-exact checkpoint readback; saved results support only their recorded
time and scope. When reporting tests, include the exact command and output,
candidate SHA, code/input/dependency bindings, and scope. Reuse unchanged,
hash-bound results only within their declared scope and freshness limits; rerun
when relevant changes, failures, or unresolved risks invalidate prior proof, or
required final-candidate checks remain. Never present an older result as a new
candidate pass.

For independent captures, use a new output directory for every observation, even
of the same build; reuse capture code, never frozen output paths. Follow the
[capture evidence rules](../../../docs/firecrawl-ops/references/development-and-runtime-checkouts.md#independent-verification-captures).

New source attempts make retained rejected artifacts private and record
`rejected_artifact_sha256` beside the existing `rejected_artifact` path. No
output means no digest. Older records may lack this field; a later hash cannot
retroactively attest their original bytes or reconstruct missing HTTP bodies.

## Architecture and ownership map

`collect.ts` dispatches source adapters in `sources/`. Each adapter owns its
source-specific enumeration and detail qualification; shared fetch, parsing and
bounded context helpers live in `lib/`. Enumeration identifies candidates; detail
collection can enrich or reject them. Neither an observed candidate nor retained
source context alone establishes a canonical listing or complete coverage.

Supervised refresh and series entrypoints own operational checkpoints, receipts,
locks and recovery. `source_evidence/` owns private acquisition observations,
planning and admitted evidence append, with its own custody and replay contracts;
it does not own canonical ingest. `document_text/` converts caller-supplied local
files and reports extraction outcomes, while the source-evidence bridge owns
bounded document acquisition and sealing. The canonical path remains validation
and source/aggregate admission followed by `cre_ingest.py` and exact readback.
Monitor artifacts are sparse and follow their separate observe-only path, never
canonical ingest. Each module's scoped instructions and README own its details.
This map describes code boundaries, not deployment or production acceptance.

## Read order

For collector execution or test work on a fresh machine or clone, follow
[`SETUP.md`](SETUP.md) before collection or testing. Run collector suites only
from the standalone `git clone --no-hardlinks` described in its
[isolated verification recipe](SETUP.md#isolated-verification). Documentation-only
work needs no environment setup or provider smoke.

The supervised refresh path is `collect.ts` and the relevant `sources/` adapter
→ artifact validation → source coverage gate and ingest dry run → aggregate
coverage gate → admitted `cre_ingest.py` write → generation-exact validation
readback. Some admission paths require an additional sealed dry run.
`cre_checkpoint_refresh.py` coordinates these stages; pipeline mode can overlap
collection with serial, separately gated writes. This map describes code flow,
not source acceptance or permission to run it. Use `START_HERE.md` for the
procedure and gates, `sources/AGENTS.md` for adapter contracts,
`lib/AGENTS.md` for shared primitives, and `tests/AGENTS.md` for test boundaries.

1. Use the applicable current procedure in [`START_HERE.md`](START_HERE.md); for
   refresh work, start with [Strict listing refresh](START_HERE.md#strict-listing-refresh).
   Read [bounded resource recovery](START_HERE.md#bounded-resource-recovery) and
   [freshness evidence SLO](START_HERE.md#freshness-evidence-slo) only when those
   procedures are relevant. Its [Operational Recovery](START_HERE.md#operational-recovery)
   section is gated Mac-tier recovery guidance; consult it only for that host
   path and use current host and task evidence before acting.
2. Read this file for collector, ingest, data-ownership and lifecycle contracts.
   For source work, read [`sources/AGENTS.md`](sources/AGENTS.md) and the
   affected source contract. Read `lib/AGENTS.md` or `tests/AGENTS.md` only when
   changing those modules.
3. For brokerage coverage or website variation, follow the
   [completion playbook](../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md),
   especially its [current-blocker path](../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md#choose-the-next-step-from-the-current-blocker)
   and [listing-fidelity standard](../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md#comprehensive-listing-fidelity).
4. Read the current owning Linear records for nontrivial work:
   [AGENTIC-2902](https://linear.app/agenticassets/issue/AGENTIC-2902) owns source
   blockers and remaining-source acceptance; [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752)
   owns current runtime/task pins and operation settlement. Use
   [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895) for the
   parent plan, measurements and learning log; reuse its existing experiment
   and acceptance children.
5. Use subsystem guides only when relevant:

   | Work area | Guide |
   | --- | --- |
   | Collector architecture or EQUIRE consumption | [CRE system design](../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md); [EQUIRE consumer API](../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md) |
   | Monitor or gate work | [Monitor subsystem](../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md); [historical first-apply handoff](HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md) only for that legacy history |
   | DAUM partial acceptance | [DAUM verified subset](../../../docs/firecrawl-ops/references/daum-verified-subset.md); a subset never proves complete brokerage coverage |
   | Private acquisition evidence and extracted listing-document text | [`source_evidence/AGENTS.md`](source_evidence/AGENTS.md), [`source_evidence/README.md`](source_evidence/README.md); local conversion: [`document_text/AGENTS.md`](document_text/AGENTS.md) |
   | Throughput or matched measurements | [CRE performance telemetry](../../../docs/firecrawl-ops/references/cre-performance-telemetry.md); distinguish staged from verified counts and use AGENTIC-2895, not an issue per run |
   | Enrichment worker or legacy Mac tiers | [Historical enrichment worker design](ENRICHMENT_WORKER_DESIGN_2026-06-15.md); [`launchd/AGENTS.md`](launchd/AGENTS.md) for that host path |
   | Windows scheduled operation | [Guarded tier guide](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md#windows-task-scheduler-installation-and-acceptance) and current AGENTIC-3752 evidence |
   | CRE notifications | [`slack-setup.md`](slack-setup.md) for the private webhook and delivery checks; root Docker `.env` does not configure native Windows task notifications |
   | Host/platform history | [June hosting options](../../../docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md), a historical decision aid |
   | Phase-2 status-activation history | [Phase-2 board impact](../../../docs/firecrawl-ops/references/cre-phase2-board-impact-2026-06-13.md) |

   Read other architecture, consumer, or host guides only when that boundary is
   in scope.

`BROKERAGE_STATUS_2026-06-12.md` and dated handoffs are historical evidence.
Consult them only for a specific historical discrepancy, legacy recovery, or
the subsystem they document; use current START_HERE procedures and Linear
evidence for present operation and source status.

## Standing execution priorities

Use the [parallel-work and throughput instructions](../../../docs/firecrawl-ops/references/cre-performance-telemetry.md#standing-parallel-work-and-throughput-instructions)
on every substantial CRE phase: delegate independent work, test in parallel,
keep collection moving while database units run, and avoid repeating verified
work. Investigate shared provider/adapter families before writing brokerage-only
fixes; follow `sources/AGENTS.md` for the scope and regression checks.
For delegated work, use Sol at medium reasoning and Luna at high or extra-high
for smaller bounded tasks, unless the user explicitly chooses another setting.
After major code phases, use the repository's
[thermo-nuclear code-quality review](../../../.agents/skills/thermo-nuclear-code-quality-review/SKILL.md)
and resolve confirmed findings before shipping.

## Files

| File / dir | Purpose |
|------------|---------|
| `collect.ts` | Orchestrator: 51 sources, CLI, broker merge, artifact write |
| `types.ts` | Shared listing vocabulary + `SourceResult` (`truncated?`, etc.) |
| `sources/` | Per-broker adapters - see `sources/AGENTS.md` |
| `lib/` | Shared scrape/config/util - see `lib/AGENTS.md` |
| `source_evidence/` | Private append-only capture planning, admitted append and text search; no canonical listing, lifecycle, normalized fact or OM-fact writes |
| `document_text/` | Pinned local AnyDoc adapter with explicit extraction/OCR-needed/failure artifacts; no automatic download or hosted upload |
| `cre_ingest.py` | Collector JSON → `credeals` upsert (stdlib + psql) |
| `cre_image_transition_log.py` | Shared stable-file/hash mechanics for post-commit image receipts; no source qualification policy |
| `cre_colliers_image_transition.py` | Existing Colliers-main seven-field-bound image receipt and source-specific qualification |
| `cre_transwestern_image_roles.py` | Validate Transwestern raw role evidence against final staged images |
| `cre_transwestern_image_sql.py` | Transwestern in-transaction prior-image checks/projection and full image-state digest query |
| `cre_transwestern_image_outcome.py` | Validate the eight-field-bound Transwestern receipt, raw reports and image-state readback |
| `cre_monitor.py` | Observe-only diff/event runner (007 tables); never writes `status`/`deleted_at`; enqueues new/changed into `cre_enrichment_queue` |
| `cre_gate.py` | Per-source coverage gate (`cre_source_baseline`); emits `mark_missing_safe` rollup |
| `cre_enrich.py` | Tier-B queue worker: claims a batch from `cre_enrichment_queue`, runs `collect.ts --enrich-input` (targeted detail), re-ingests additively (`cre_ingest.py --in`), deletes done rows. Additive by construction (never `--mark-missing`/`--activate-status`); URL-matched, id-keyed completion; pure builders + thin `run()`. Manual recovery may use `--source SOURCE_KEY` for an exact queue-claim filter: only that source can be claimed, retried, or have attempts incremented. Draft guarded runs bind fixed private output paths in their existing receipt; direct runs use unique private directories. See `source_evidence/AGENTS.md` for local custody, separate from database retention. Requires separate production acceptance. |
| `cre_daily_update.sh` | Legacy monolithic backstop: healthcheck → full collect → gate [3/4] → ingest → prune `out/daily/` artifacts; not the default supervised or scheduled route |
| `cre_status.sh` | Read-only run-health heartbeat: launchd state, per-tier staleness vs cadence, last-run verdict (from `out/daily/last_run_<tier>.json`), last-ingest counts, checkout branch/HEAD/dirty state, per-tier rendered-plist drift, `out/` footprint + lock state (hung/stale), stack/env/TCC. Exits nonzero if unhealthy. `--full-health` runs the full healthcheck; `--expected-sha <commit>` enforces the clean deployment identity gate |
| `cre_code_handoff.py`, `cre_code_restoration_evidence.py`, `cre_code_restoration_successor.py` | Reviewed code handoff and finite held-task restoration. Restoration preserves failed-A provenance, binds the one-time catch-up decision/current fleet, and separates maintenance release from scheduled execution. After release-start, failures are observation-only; an active scheduled owner needs an authentic native/WSL/Linux/control/authority join. See the development/runtime checkout guide. |
| `cre_setup.sh` | One-command preflight + bootstrap for a fresh clone (toolchain, deps, env, optional network smoke); run first. See `SETUP.md` |
| `cre_validate.py` | Post-ingest Supabase validation (`npm run validate:supabase`); not in daily script |
| `cre_repair_colliers_derived_psf.py` | One-row, exact-generation repair for the 2026-09-10 Colliers derived-PSF regression. Read-only by default; apply requires the canonical filesystem and database lifecycle locks, a rollback-proved transaction, and owner-only pre/post/rollback evidence. |
| `cre_repair_jll_images.py` | Dedicated exact-manifest JLL malformed image-reference repair and restoration. Default read-only; [operator guide](../../../docs/firecrawl-ops/references/jll-image-reference-repair.md) covers private evidence, transaction checks and recovery. |
| `backfill_media_from_raw_data.py` | One-time additive lift of media/docs already stranded in `raw_data` into `cre_listing_media`/`cre_listing_links`/`cre_listing_documents`; `--dry-run` default, `--apply` gated on go-ahead; `011` DDL now applied 2026-06-15 (only the media backfill RUN remains gated). See `HANDOFF_MEDIA_CAPTURE_2026-06-15.md` |
| `cre_parse.py` | Python mirror of `lib/parse.ts`: shared CRE text parsers (price/sqft/cap-rate/address). Imported by `cre_ingest.py` and the raw_data backfill; verifiably identical to the TS side via a shared golden test-vector table |
| `cre_geo.py` | Offline ZIP->county+CBSA crosswalk resolver (`data/zip_cbsa_crosswalk.csv`). `ZipCbsaCrosswalk` + `derive_geo()`; used by `cre_geo_backfill.py` and optionally `cre_ingest.py`. Pure, stdlib-only, no network |
| `cre_backfill_raw_data.py` | One-time additive/idempotent Class-1 scalar backfill from `raw_data` into `cre_listings` (canonical_url + institutional cols). `--dry-run` default, `--apply` gated; APPLIED 2026-06-15 (canonical_url 0->87,324, 0 decode failures). Never touches `status`/`deleted_at` |
| `cre_geo_backfill.py` | Additive/idempotent geo derivation (county/cbsa/geo_source) for existing rows via `cre_geo`. `--dry-run` default, `--apply` gated; APPLIED 2026-06-15 (85,618 of 87,328 rows) |
| `om_classify_existing.py` | One-time additive re-classification of `doc_type='brochure'` rows into flyer/floor_plan/om/financials. `--dry-run` default; APPLIED 2026-06-15 (14,087 of 70,414 upgraded). Upgrade-only, never downgrades |
| `om_url_resolver.py` | Resolves viewer-wrapped / non-`.pdf` brochure URLs (Buildout iframe, DocumentCloud, etc.) to the real `.pdf` document URL for parser regression coverage |
| `om_parse.py` | Retired Firecrawl OM writer. Pure extractors and dry-run artifacts remain for regression coverage; `--apply` exits `78`. GetCREdata is the sole production OM extraction writer. |
| `run_colliers_main_full.sh` | Resumable colliers-main batch driver (~15,896 URLs) |
| `launchd/` | Legacy macOS tier templates and installer (`*.plist.template`, `install_launchd.sh`); scheduler state is host-local and must be freshly verified - see `launchd/AGENTS.md` |
| `tests/` | pytest contracts - see `tests/AGENTS.md` |
| `SETUP.md` | Fresh-clone setup runbook for macOS and Ubuntu/WSL2: `cre_setup.sh`, env, macOS launchd generator |
| `START_HERE.md` | Current procedures and new-session runbook |
| `BROKERAGE_STATUS_2026-06-12.md` | Historical per-broker coverage snapshot |
| `HANDOFF_COLLIERS_MAIN_2026-06-13.md` | colliers-main full detail run handoff |
| `HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` | Monitor hardening, module split, first `--apply` seed |
| `SECURITY_REVIEW_2026-06-14.md` | Branch security review: verdict, the `standard_conforming_strings` pin fix, deferred base-table REVOKE |
| `ENRICHMENT_WORKER_DESIGN_2026-06-15.md` | Tier-B enrichment-queue worker + cadence restructure (monitor 2x/day, enrich every 4h, weekly additive full backstop, daily retained for rollback). Implemented in code; live scheduler recovery remains gated. |
| `HANDOFF_MEDIA_CAPTURE_2026-06-15.md` | Capture all videos/links/docs/images + full markdown + stranded structured fields. Generic harvester (`lib/harvest.ts`), richer scrape formats, NEW `cre_listing_media`/`cre_listing_links` tables (`sql/011`), Buildout-iframe Tier-B detail for lee/svn, raw_data backfill. BUILT + verified in code; live apply GATED |
| `PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md` | Phase-2 data-lift implementation contract: the spec the `011`-`014` DDL, the backfills, and the `cre_parse`/`cre_geo`/`om_*` modules implement |
| `HANDOFF_DATA_LIFT_2026-06-15.md` | Phase-2 data-lift handoff: what shipped (DDL, three additive backfills, OM-parse tier), the prod apply log, and test counts |
| `RAW_DATA_GAP_CLASSIFICATION_2026-06-15.md` | Which structured fields/media/docs are stranded in `raw_data`, plus the document-corpus audit that scopes the backfills |
| `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md` | Per-column fill-rate report for `cre_listings` (drives backfill targeting); raw outputs in `reports/` |
| `FRESHNESS_HISTORY_REVIEW_2026-06-15.md` | Freshness/accuracy/historic-retention review (the H/M/L findings behind `009` and the ingest-written price history) |
| `TODO.md` | Collector working TODO list |
| `data/` | Geo crosswalk reference (`zip_cbsa_crosswalk.csv` consumed by `cre_geo.py`) + `build_zip_cbsa_crosswalk.py` builder and `README.md` |
| `reports/` | Coverage report outputs (column/brokerage/transaction CSVs + summary JSON; see `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md`) |
| `workflows/` | Historical Workflow scripts; `cre_enrichment_worker.workflow.js` preserves build/test/review provenance and refuses database or scheduler cutover |
| `archive/` | Dated buildout history (see `archive/README.md`) |
| `../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` | Architecture + go-forward plan (§14) |
| `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` | Monitor run model and operational gotchas |
| `../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md` | How EQUIRE reads the data |
| `../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md` | Brokerage upgrade process |
| `../../../docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md` | Where to run the pipeline (cloud vs Mac mini): platform comparison, anti-bot IP risk, recommendation (decision aid, not actioned) |
| `out/` | Run artifacts (gitignored) |

## Quick start

```bash
cd scripts/firecrawl-ops/cre_collector
bash cre_setup.sh                # fresh clone: preflight + bootstrap (deps, checks, smoke). See SETUP.md
npm run typecheck                # TypeScript validation
bash cre_status.sh               # macOS scheduler/artifact preflight
# Ubuntu/WSL: use bash cre_status.sh --scheduler external instead.
```

Setup's default smoke is a bounded public Interra inventory GET followed by
database-free dry-run ingestion; it is not an offline or full-refresh test.
Run Python and TypeScript suites only in a genuine standalone
`git clone --no-hardlinks`, following
[SETUP's isolated verification recipe](SETUP.md#isolated-verification).
Use [START_HERE](START_HERE.md) for the supervised checkpoint path; raw
`--source=all` and the legacy daily script are not routine setup checks.

## collect.ts

Flags: `--source=all|csv` `--transaction=sale|lease|both` (default `both`)
`--max-items` (0 = unlimited) `--page-cap` (default 60; use 400 for full runs)
`--concurrency` (1–6, default 3) `--out=path` `--monitor` (enumeration-only pass)
`--enrich-input` (targeted detail enrichment from `cre_enrich.py` queue batch).

Env: `FIRECRAWL_API_URL` (default `http://localhost:3002`);
`FIRECRAWL_API_KEY` (optional; defaults to `local-self-hosted` when unset).

**51 source keys** (sale/lease support, methods, monitor matrix, Buildout rules,
`external_id` gotchas, per-source env vars): `sources/AGENTS.md`. Obtain current
row counts from a fresh validator or exact checkpoint readback. The matrices in
`START_HERE.md` and `BROKERAGE_STATUS_2026-06-12.md` are dated history.

**`--page-cap`** bounds pagination on `jll`, `cbre-dealflow`, `colliers`
(SalesTracker), `nai-global` (`PAGE_CAP × page size`), and Savills sale pages.
It does not cap API-paginated or sitemap-driven sources (`cushman-wakefield`,
`transwestern`, `marcus-millichap`, `colliers-main`, Buildout).

**Scrape primitives** (`scrapeJson`, `brokerRef`, `pmap`, `prune`): `lib/AGENTS.md`.

## cre_ingest.py

For common source-field qualification and atomic value/provenance updates,
read [`source_fields/AGENTS.md`](source_fields/AGENTS.md) and its README before
changing field mappings, conflict resolution or freshness behavior.

Maps collector JSON to `credeals.cre_listings` (+ contacts/documents/images
children, + `cre_scrape_jobs` row per brokerage). Stdlib only; talks to Postgres
via psql (`PSQL_BIN`, else `/opt/homebrew/opt/libpq/bin/psql`,
`/usr/local/opt/libpq/bin/psql`, then `PATH`). Document and image child rows
store external URLs only.

Credentials: reads `POSTGRES_URL_NON_POOLING` (preferred) or `POSTGRES_URL`.
Discovery order: `--env-file` flag, then `CRE_ENV_FILE` env var, then the
`~/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local`
default (fallback `~/Documents/GitHub/agentic-assets/CRE_EQUIRE/.env.local`).
Set `CRE_ENV_FILE` on any machine where the EQUIRE repo is not at the default
`~/Documents` path (production Mac mini uses `~/.config/cre/equire.env` via
launchd). `cre_monitor.py` and `cre_gate.py` import the same loader.
Live runs print only the env file path, never the URL.

Key behavior:
- Dedup key `(brokerage_id, external_id)`; sub-sources fold into the parent
  brokerage with prefixed ids (`dealflow:`, `investor:`, `main:`). Missing ids get
  `url:<sha1-16>` from the listing URL; Buildout (`svn`, `lee-associates`)
  prefers URL `propertyId` with `-sale`/`-lease` stripped first. Buildout source definitions live in `sources/buildout-registry.ts` and the
  existing `BUILDOUT_SOURCE_INVENTORY_OPTS` in `sources/buildout.ts`.
- Sale + lease passes merge to `transaction_type='sale_or_lease'`.
- Provider cards without a canonical listing URL are not written to
  `cre_listings`. Complete CBRE Deal Flow and Colliers SalesTracker snapshots
  persist them as explicitly provisional `cre_source_index` rows under
  source-specific namespaces and watermarks. Colliers canonical ProjectIds are
  one-to-one and never merge, including identical duplicates.
- `cap_rate` stored as a decimal fraction (e.g. `0.065`); `norm_cap_rate` drops
  non-numeric, `<= 0`, percent inputs `>= 30`, and `>= 0.5`. Upsert uses
  `COALESCE` so a null new cap rate keeps the existing DB value. Price columns
  (`sale_price_usd`, `sale_price_per_sf`, `lease_rate_min`, `lease_rate_max`)
  also COALESCE-keep: a transient parse miss does not blank a previously-good
  numeric price. Prior price/SF must remain in `(0, 10000]` and prior lease
  rates in `(0, 500]`, matching their existing forward-normalization bounds.
- **An explicit source withholding control is different from a parse miss.**
  Source-bound JLL and Avison projections override ordinary price retention;
  do not restore a withheld value through sale/lease merge, current raw data,
  Markdown or retained child titles. Avison's exact numeric `hide_price=1`
  controls sale asking and sale PSF only; preserve independently supplied rent,
  cap rate, NOI and other useful facts. Absent/visible controls retain ordinary
  behavior; malformed or contradictory controls refuse. The implementation is
  source-owned in `cre_avison_price_visibility.py`, with source hooks in
  `sources/avison-young.ts`. Follow the
  [visibility and freshness guidance](../../../docs/firecrawl-ops/references/cre-ingest-freshness-troubleshooting.md#source-visibility-is-not-missing-data)
  and the source contract; never borrow another brokerage's marker or promote
  a failed detail observation to fresh data.
- Upsert sets `status='active'`, resurrects soft-deleted rows (`deleted_at=NULL`),
  but only resets status to `'active'` when the prior status was `'inactive'` (a
  mark-missing soft-delete); a real terminal (`sold`/`leased`/`off_market`) that
  flickered back into a feed keeps its prior status label. Wholesale-replaces
  contacts/documents/images **unless**
  `jsonb_path_exists(raw_data, '$.**.detailError')` (preserves children on
  transient detail failures).
- **Bulk lifecycle locking:** every generated ingest transaction takes exactly
  one transaction-wide lifecycle advisory lock, then locks existing source-index
  rows before listing rows in deterministic order. Never add one transaction
  advisory lock per listing identity: high-volume sources exhaust PostgreSQL's
  shared lock table before mutation. The global lock also serializes the bounded
  reviewed reconciliation path; unique identity constraints serialize new-row
  conflicts.
- **Transwestern image transition:** the source-specific
  opt-in path binds one strict-fresh full-source artifact, generation/start,
  database target, ingest attempt and clean collector code SHA. Its single
  ingest transaction snapshots prior image rows, preserves each protected
  URL occurrence and multiplicity, and permits only source-proved role changes
  plus narrow established template/tile exclusions. Primary selection prefers
  a retained current feed/gallery witness, otherwise keeps the prior primary
  only if that exact image remains; absence of a witness does not invent one.
  The receipt is emitted only after `COMMIT` and carries a full physical image
  count/digest for subsequent readback. This exception applies only to
  Transwestern's image-count comparison: the normal full-14 validation queries
  and all other quality/source/lifecycle guards remain in force. Code merge
  and isolated QA do not establish runtime promotion or full-source production
  acceptance; use the current evidence on
  [AGENTIC-4576](https://linear.app/agenticassets/issue/AGENTIC-4576).
- **Protected lifecycle reconciliation:** use the optional finite four-parent
  JLL Investor contract in the [lifecycle runbook](../../../docs/firecrawl-ops/references/cre-listing-lifecycle-runbook.md#optional-four-parent-jll-investor-protection).
  Its context is part of the exact plan hash and must never be ignored or
  downgraded. Compare all protected objects and catalog before job DML, and
  finalize the two actions and job in one transaction. Ordinary multi-batch
  reconciliation retains its existing behavior. A stale preimage or uncertain
  commit requires read-only diagnosis, not blind replay or a replacement runner.
- **Dead-owner recovery:** an exited PID never makes an armed recovery fence
  safe to clear. Follow [lock authority and recovery](LOCK_AUTHORITY_RECOVERY.md)
  for the explicit known-settled operator procedure. Reconcile database and
  remote outcomes, prove descendant settlement, and bind the exact filesystem
  state before recovery; preserve its forensic evidence and persistent authority
  inode. Recovery does not retry an apply or certify source freshness.
- **Status activation is OPT-IN and default-OFF.** Source-derived statuses are
  suppressed unless `--activate-status` is passed on the CLI or
  `CRE_ACTIVATE_STATUS=1` is set in the environment. New helpers:
  `_status_activation_enabled()` and `apply_status_activation_gate()`. When off,
  the upsert inserts `COALESCE->'active'` and the activation UPDATE is a no-op.
  Do NOT assume status activation fires on the next daily/manual full ingest;
  it requires the explicit flag plus consumer-side gate deploy first.
- **Ingest-written value history (2026-06-15, applied to prod):**
  `build_sql()` now captures a `_prior_vals` temp table of watched fields BEFORE
  the upsert, then writes one row to `cre_listing_price_history` per listing
  where a watched field (`sale_price_usd`, `sale_price_per_sf`, `lease_rate_min`,
  `lease_rate_max`, `status`, `cap_rate`) IS DISTINCT FROM its prior value.
  First-ever inserts produce no history row (history starts at the first CHANGE).
  The INSERT is existence-guarded via `to_regclass` (no-op if `009` is absent).
  Dry-run emits the plain unguarded INSERT so tests can assert the shape.
- **Contacts + documents archived at retirement (2026-06-15, applied to prod):**
  When `--mark-missing` fires, the soft-delete block first captures retired rows
  in a `_retired` temp table (with `prior_status`), then runs the UPDATE, then
  INSERTs one `cre_listing_events` row per retired listing with
  `event_type='disappeared'` / `source_value='mark_missing'`, and then (guarded)
  snapshots the retired listings' final contacts and documents into
  `cre_listing_contacts_archive` and `cre_listing_documents_archive`. Images are
  excluded (high volume, low historical value). These tables have no FK to
  `cre_listings` so they survive a future hard delete of the source row.
- **`--mark-missing`: soft-deletes unseen rows (`status='inactive'`,
  `deleted_at=now()`).** Per brokerage, only when every source pass for that
  brokerage ran error-free, staged `>= --mark-missing-floor` (default 100), and
  **folded coverage is count-aware and complete**: every folded key (e.g. `cbre` +
  `cbre-dealflow`, `jll` + `jll-investor`, `colliers` + `colliers-main`) must
  appear in the artifact AND have a nonzero `listingsCollected` count
  (`discovered_by_source_key` dict built from `source_entries`). Singletons still
  short-circuit on `len(known_keys) == 1`.
  Never use on partial/subset runs.
- `--dry-run --keep-artifacts DIR` writes SQL without connecting.

Date semantics (ingest scope):
- `listing_date`: not populated by bulk collector; leave null unless a source
  proves a true first-listed/on-market field.
- `updated_date` ← `listing.lastUpdated` (YYYY-MM-DD prefix only).
- `scraped_at` prefers the admitted `detailObservedAt`; a
  source-revision-cache row uses its current-generation `validatedAt`; an
  authoritative-inventory-feed row uses `inventoryObservedAt`. Artifact
  `finishedAt` is a legacy fallback only. Generation-exact readback, not
  `scraped_at` alone, proves current-source freshness.
- `created_at`, `updated_at`, `deleted_at`: database lifecycle fields.

Supabase access: `credeals.cre_*` tables and `v_cre_*` views are service-role
only. Read `archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` before changing grants
or view privileges. Consumer API details: `cre-equire-consumer-api.md`.

Phase-2 data-lift applied to prod 2026-06-15 (all additive, board-unchanged):
DDL `011`-`014` (media/links tables, institutional + geo columns, OM-facts and
zip/CBSA crosswalk tables) plus three additive backfills (`cre_backfill_raw_data`,
`om_classify_existing`, `cre_geo_backfill`); `status` was never touched. Current
coverage requires fresh validation and exact checkpoint-manifest readback; record
execution evidence on [AGENTIC-2902](https://linear.app/agenticassets/issue/AGENTIC-2902).

## Authorized maintenance

Cayman's [standing CRE maintenance direction](https://linear.app/agenticassets/issue/AGENTIC-2895#comment-e988227e-0ff0-482b-941b-5d4220d3921c)
authorizes well-researched, well-tested fixes within this pipeline task. Proceed
through implementation, meaningful regression tests, independent review and the
normal release procedure without repeatedly asking for routine design approval.
Keep exact production target/manifest/preimage and recovery checks, source/data
ownership, and the instruction to leave the Mac setup unchanged. This does not
authorize a broader deletion predicate, relaxed validation or unrelated work.

## Major-phase code quality review

After a major implementation or phase completes, have a separate agent apply
the [thermo-nuclear code quality review skill](../../../.agents/skills/thermo-nuclear-code-quality-review/SKILL.md)
before merge or deployment. Prioritize high-conviction structural simplification,
decomposition and canonical helper reuse. Fix substantive findings with focused
regression proof; do not substitute cosmetic rewrites or relax tests.
Record confirmed/refuted findings and rationale on the owning Linear issue or PR,
with learning pointers in [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).
Small mechanical edits need a focused review, not a repeated major-phase ceremony.

## Monitor mode (hard rules)

`collect.ts --monitor` writes the same artifact shape with `runMeta.mode="monitor"`.
**Never feed a monitor artifact to `cre_ingest.py` (especially not with
`--mark-missing`).** Monitor artifacts go to `cre_monitor.py` only;
`cre_gate.py` reads baselines for weekly `mark_missing_safe`.

- Enumeration `external_id` must match full-collect / ingest keys (enforced by
  `tests/test_enum_key_invariant.py`).
- **`cre_monitor.py`** refuses disappearance events for sources whose enumeration
  pass reported `error` or `truncated` (not overridable by `--force-disappear`).
- Per-source monitor behavior (excluded sources, supersets, detail skips):
  `sources/AGENTS.md` + `cre-monitor-subsystem.md`.
- The legacy macOS monitor tier uses default `--page-cap=60` unless overridden;
  see `launchd/AGENTS.md`. This is not the current Windows scheduled path.
  Scheduler state is host-local and can change without a repository commit.
  Run `bash cre_status.sh` as the read-only preflight, then follow the current
  operator procedure before a recovery attempt. `cre_gate.py` is wired into
  `cre_daily_update.sh` as observe-only step [3/4]
  (`--in RUN --apply --strict --out gate.json`); if the strict gate detects any
  partial/regressed source, the script auto-downgrades to `--no-mark-missing`.

## Legacy daily-update backstop

`cre_daily_update.sh` is retained for explicitly approved legacy recovery; it
is not the default supervised refresh or scheduled entrypoint. Its behavior is
healthcheck → full collect (sale+lease, unlimited,
`CRE_PAGE_CAP` default 400) → observe-only gate [3/4] → ingest → prune (EXIT
trap; keeps 14 `run_*.json`, 29 `run_*.log`, 14 `gate_*.json` under
`out/daily/`). Script default includes `--mark-missing`; use
`bash cre_daily_update.sh --no-mark-missing` for additive legacy recovery. The
supervised serial checkpoint-series path, opt-in pipeline mode, and guarded
Windows wrapper are documented in `START_HERE.md` and the
[guarded tier guide](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md).
Use `--mark-missing` only under the separate coverage and lifecycle conditions
in [`START_HERE.md`'s legacy backstop guidance](START_HERE.md#legacy-scheduled-backstop).

Historical macOS tier schedules: `launchd/AGENTS.md`. Never infer whether they
are loaded from this file; use `bash cre_status.sh` and `launchctl list` on the
target host. Step [3/4] runs `cre_gate.py` observe-only.

For an operator-requested full freshness sweep, prefer
`cre_checkpoint_refresh.py` over the monolithic daily runner. It checkpoints
each source, binds observations to an immutable generation, bypasses Firecrawl
response caches for strict source reads, expires resumable generations after
24 hours and validates artifact provenance. Serial mode stops on a refused
source gate; all configured artifacts must pass their source gates
and the aggregate gate before live ingest begins. In opt-in pipeline mode,
each sealed unit must pass its own source and aggregate gates before its
serial database transaction; another source may continue collecting.
Before sealing, exactly bound standard `hold`/`first_seen` refusals become
terminal source failures while other eligible sources continue. Invalid or
uncertain evidence and sealed-unit refusals remain globally terminal; see
the pre-seal and retained-artifact accounting contract in `START_HERE.md`.
Ordinary `first_seen` requires an explicit
reviewed baseline seed and readback before resuming the same immutable run.
Explicit current-inventory admission preserves the absent baseline and raw
verdict while requiring each source's complete fresh proof. Interra, registered
Buildout feeds and DAUM support exact source-keyed pipeline admission; serial
admission stays singleton. Pyramid and Essex also allow explicit source-bound
entries in the original flat pipeline owner; serial admission remains singleton.
Each requires its own complete fresh proof and absent-baseline checks. Mixed
code support does not establish fresh mixed-run acceptance or inherit another
member's authority.
This grants additive inventory scope, never historical-baseline certification
or lifecycle authority. Follow the source contracts in [START_HERE.md](START_HERE.md)
and [sources/AGENTS.md](sources/AGENTS.md); Buildout details remain in the
[admission reference](../../../docs/firecrawl-ops/references/buildout-current-inventory-admission.md).

The checkpoint runner has fail-closed Darwin/Linux CPU and memory guards and writes resumable
evidence. For exact thresholds, resource-profile setup, guard and incident
logs, full-registry commands, and stale-parent recovery, follow
`START_HERE.md` rather than duplicating operational settings here. Serial
mode uses one collection slot. Opt-in pipeline mode defaults to two;
`--source-workers` adjusts its collection slots independently of the single
database lane. Preserve provider pacing and source-specific admission gates.
`cre_checkpoint_series.py --sources all` is the serial full-registry path;
source-local failures may continue, while resource-guard and global failures
stop the series. Every listing write remains inside
`cre_checkpoint_refresh.py`.

The strict source set is `cbre`, `jll`, `jll-investor`, `colliers-main`,
`cushman-wakefield`, `svn`, `lee-associates`, `franklin-street`, `newmark`, `savills`,
`transwestern`, `marcus-millichap`, `nai-global`, `matthews`, `srs`, `hanley`,
and `kidder-mathews`. `cbre-dealflow` and `colliers` may emit explicitly
inventory-only provider cards. `avison-young` proves inventory and property
detail freshness while preserving existing contacts when its supplemental team
feed is unavailable; do not claim fresh contacts in that state.

Child admission has explicit source classes. CBRE replaces its
collector-owned children. All Buildout feeds, Interra Realty, Cushman &
Wakefield, SRS, Hanley, Kidder Mathews, and Newmark preserve existing children.
Every other strict source must provide an admitted current detail observation
and must not preserve children after a detail failure. The precommit child
guard compares only the same pre-existing parents that remain active, so
legitimate mark-missing retirement is not mistaken for physical child loss;
less than 70% retained for a child type with a baseline of at least 10 aborts
the transaction.

Use the checkpoint runner's generation-exact database readback as the write
admission proof. `cre_refresh_report.py --since <run-start>` is the broader
scope report; neither that report nor a fresh `scraped_at` proves that an old
detail cache was refreshed.

The checkpoint-series publisher advances
`out/checkpoint-series/producer-source-health.json` only after a complete
all-source series. Its `producer-freshness-v2` inventory generation binds each
required source to the post-ingest readback of active row count, maximum
`cre_listings.updated_at`, maximum observation/enumeration clock, and complete
publication status. Partial, failed, running, and interrupted series never
replace the last-good receipt. The generation ID is derived from the canonical
fingerprint digest, so an exact retry reuses it and any changed tuple does not.

## Adding a source

1. Implement `srcNewSource(tx, max, monitor)` in `sources/<name>.ts` returning
   `SourceResult`; register in `collect.ts` (`SOURCE_KEYS` + `runSource`).
2. Map its key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` (add seed row in
   `../sql/001_cre_brokerages.sql` and apply).
3. Probe: `npx tsx collect.ts --source=<key> --transaction=both --max-items=6`,
   then `cre_ingest.py --dry-run`. Adapter contract, monitor support, env vars:
   `sources/AGENTS.md`.

## Supported execution environments

CRE runtime: macOS or Linux (including Ubuntu WSL2); native Windows Python is
unsupported because lock/process-group semantics are required. Use the separate
ext4 WSL clone, never Windows node_modules or a worktree sharing a live lock.
Firecrawl API helpers can run from native Windows. Linux telemetry support does
not grant scheduler, production-write or sole-writer cutover approval. Follow
START_HERE.md for the current guard contract and SETUP.md for installation.

## Opt-in pipeline contract

Use [Start Here](START_HERE.md#opt-in-stage-overlap) for the shared macOS/Linux foreground pipeline. `--source-workers` controls brokerage collection slots (1–4, default 2 when `--pipeline` is selected), separately from acquisition concurrency; one refresh owner and serial database transactions remain mandatory. Windows only forwards these portable settings. Serial resume requires identical configuration and admissions. Recorded pipeline state refuses mixed-stage resume; reconcile the exact generation and any uncertain writes before another invocation.
