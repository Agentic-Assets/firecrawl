# CRE Collector Setup

How to prepare the CRE listing pipeline on macOS or Ubuntu in WSL2. Linux
checkouts and dependencies belong on ext4, separate from the native Windows
checkout. `cre_setup.sh --check` checks prerequisites; setup success does not
authorize production collection, database writes, scheduling or writer cutover.

The pipeline has two halves:

- **Heavy half** (`collect.ts`, Node/tsx): scrapes brokerage sites through the
  local self-hosted **Firecrawl Docker stack** (API + headless Chromium + Redis
  + RabbitMQ + queue). Needs the stack up and a residential IP.
- **Light half** (`cre_ingest.py`, `cre_monitor.py`, `cre_gate.py`, Python
  stdlib only): upserts into Supabase `credeals` by shelling out to `psql`.
  Needs `psql` and a `POSTGRES_URL`.

> **Historical operational gate, 2026-07-11:** This is a setup reference, not
> authorization to change the Mac mini. The no-scheduler statement was verified
> on that date only. Before any stack recovery, scheduler load, database write,
> or canary, run `cre_status.sh`, follow `START_HERE.md`, and preserve the
> operator runbook's explicit approval gates.

---

## TL;DR (fresh clone)

```bash
# 1. Clone on WSL ext4, or outside ~/Documents on macOS (see "Scheduling")
git clone <repo-url> ~/code/firecrawl
cd ~/code/firecrawl

# 2. Start the core stack (Docker Desktop WSL integration, or OrbStack on macOS)
# Target api so optional services, including FoundationDB, stay stopped.
docker compose up -d api

# 3. Point the ingestor at the EQUIRE database env (holds POSTGRES_URL*)
export CRE_ENV_FILE=/path/to/EQUIRE/.env.local

# 4. Bootstrap + verify (installs deps; public brokerage network smoke, no DB writes)
cd scripts/firecrawl-ops/cre_collector
bash cre_setup.sh

# 5. macOS only, optional and gated: render + install launchd schedules (NO load)
bash launchd/install_launchd.sh all
```

Expect `cre_setup.sh` to finish with `0 FAIL`. Warnings are deploy-time gaps it
tells you how to close.

---

## Prerequisites

| Need | How | Notes |
|------|-----|-------|
| Docker engine | Docker Desktop WSL2 integration on Windows; https://orbstack.dev on macOS | `default`, `desktop-linux` and `orbstack` contexts are accepted; verify the intended local engine |
| Node 24 + npm | Linux Node installation in WSL; Node 24 installer or Homebrew on macOS | `collect.ts` runs via `npx tsx`; do not share Windows `node_modules` with Linux |
| PostgreSQL client / psql | `sudo apt install postgresql-client` on Ubuntu; `brew install libpq` on macOS | Ubuntu and Homebrew paths are auto-detected; `PSQL_BIN` overrides discovery |
| python3 + curl | `sudo apt install python3 python3-venv curl` on Ubuntu; install Python 3 on macOS | runtime Python uses stdlib; curl checks API readiness |
| pytest (dev/CI only) | virtual environment in a separate `git clone --no-hardlinks` (commands below) | never run CRE tests from a worktree or runtime checkout; missing pytest is a soft WARN |
| Firecrawl core | `docker compose up -d api` from repo root | see the `firecrawl-ops` skill and platform onboarding docs below |
| EQUIRE `.env.local` | from the EQUIRE repo | holds `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL`; never committed here |

---

## `cre_setup.sh` (run this first)

```bash
bash cre_setup.sh              # full: toolchain + stack + npm install + code health + smoke
bash cre_setup.sh --check      # read-only doctor (no install, no network smoke)
bash cre_setup.sh --no-smoke   # install + checks, skip the network smoke test
bash cre_setup.sh --reinstall  # force npm install even if node_modules exists
```

It verifies the toolchain, local Docker context, Node deps (installing them if
missing), TypeScript typecheck + Python syntax, and `POSTGRES_URL` discovery.
API readiness uses only `GET /`, with a 2-second connection timeout and a
5-second total timeout; it checks the Firecrawl service identity. Override the
base URL with `FIRECRAWL_API_URL`, or the older `API_URL` (the former wins).
Readiness does not prove that workers or scraping work. `--check` never invokes
the full healthcheck, dependency installation, collection or ingest, and does
not create Python bytecode or diagnostic files. Failed typecheck and syntax
checks print their final 16 KiB of diagnostics inline. The resolved API URL is
also passed to collector subprocesses, without changing any env file.

Ordinary setup also runs a **network** smoke test: one public Interra Realty
inventory GET (bounded to 32 MiB and 60 seconds), selects at most 3 sale
listings, then runs `cre_ingest.py --dry-run`. This source makes no detail
requests and does not exercise Firecrawl's scraping engine. The dry-run builds
SQL and prints stats without connecting to the database. The smoke artifact is
removed afterward. Neither mode writes to the live database or prints its URL.
Exit code is nonzero only for hard prerequisites (toolchain, deps, code health);
API readiness, database-env and smoke failures are warnings, so `0 FAIL` alone
does not prove full system health.

Override the smoke source with `CRE_SMOKE_SOURCE=<key>` if needed (valid keys
are listed in `collect.ts`). For other sources, `--max-items=3` caps output
only; it does not bound the number of inventory requests. For a separate,
explicit Firecrawl scrape smoke (which POSTs a scrape request and accesses
example.com), run `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh` from the
repo root. Do not treat that command as a read-only doctor.

For Python tests, create an isolated clone so lock discovery cannot reach a
runtime checkout, then use a venv (also avoids Ubuntu's system-pip restriction):

```bash
# From the repository root; choose a new destination outside the runtime clone.
git clone --no-hardlinks . ~/code/firecrawl-tests
cd ~/code/firecrawl-tests/scripts/firecrawl-ops/cre_collector
python3 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests/ -q
```

On WSL, load `~/.agentic-assets/env` when canonical agent context has been
installed. Use `bash cre_status.sh --scheduler external` for artifacts and API
readiness; this mode does not verify an external scheduler. macOS retains the
default launchd checks with `bash cre_status.sh`.

---

## Database env (portable)

The ingestor (and monitor and gate, which import the same loader) resolve the
`POSTGRES_URL` in this order:

1. `--env-file /path/.env.local` (explicit flag)
2. `CRE_ENV_FILE` environment variable
3. The `~/Documents/GitHub/agentic-assets/...` default paths (this Mac's layout)

If the clone or the EQUIRE repo lives anywhere other than the `~/Documents`
defaults, set `CRE_ENV_FILE`. The URL value is never printed; only the resolved
file path appears in logs.

On an IPv4-only network, use the project's **Session pooler** connection from
Supabase's Connect panel (port `5432`), with the project-qualified username.
The existing `POSTGRES_URL_NON_POOLING` key is preferred when present despite
its historical name; it can hold a session-pooler URL. Do not leave an older
value there while updating only `POSTGRES_URL`.

Keep machine-specific credentials outside the checkout, for example
`~/.config/cre/equire.env` with directory mode `0700` and file mode `0600`, and
set `CRE_ENV_FILE` to that path. Provision through the approved local credential
workflow; never copy another computer's env file or commit connection strings.
Preserve each Mac's own connection settings.

Use `sslmode=verify-full` with `sslrootcert` pointing to the Supabase CA downloaded
from its Database settings. This verifies both the certificate chain and
hostname. Ubuntu's default public CA store may not contain the Supabase CA.
Keep `connect_timeout` bounded, and use the existing psql environment helpers
so credentials stay out of process arguments. First verify a bounded read-only
transaction and COPY OUT against the expected project; CLI Management API
success alone does not prove PostgreSQL transport. Temporary-table/COPY IN
contracts belong in a disposable local database before live ingestion.

The shared-pooler fingerprint binds the project suffix, not the role/password.
Changing connection endpoint or upgrading a historical pooled fingerprint
requires a fresh checkpoint generation, not an edited resume manifest.
See [Supabase connection guidance](https://supabase.com/docs/guides/database/connecting-to-postgres)
and [SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement).

---

## Scheduling (launchd, gated)

Schedules are macOS launchd jobs. The committed source of truth is the three
**`*.plist.template`** files; rendered per-machine plists are gitignored and
produced by the generator:

```bash
bash launchd/install_launchd.sh <monitor|enrich|weekly|daily|all> # render + install, NO load
bash launchd/install_launchd.sh --load monitor enrich weekly # only after recorded scheduler approval; daily is retired
bash launchd/install_launchd.sh --env-file /path/.env.local all   # bake CRE_ENV_FILE into the plists
bash launchd/install_launchd.sh --print daily                # preview rendered plist, install nothing
bash launchd/install_launchd.sh --uninstall all              # unload + remove installed copies
```

The generator self-locates the collector path, puts `node`/`python3` on the
job's PATH, validates each rendered plist with `plutil`, and never loads a job
unless you pass `--load`.

Tiers and their gates (full detail in `launchd/README.md`):

| Tier | Schedule | Action | Gate to load |
|------|----------|--------|--------------|
| monitor | 06:10 and 18:10 | cheap enumeration diff, observe-only by default | Technical checks plus operator-runbook gate 5 Cayman scheduler approval |
| enrich | every 4h at :30 | targeted additive queue drain | Technical checks plus operator-runbook gate 5 Cayman scheduler approval |
| weekly | Sun 03:00 | additive full backstop; soft-delete only under a separate escalation | Technical checks plus operator-runbook gate 5 Cayman scheduler approval |
| daily | retired | rollback case only; never part of the active load set | Do not load |

### macOS Full Disk Access (TCC)

> **Current operational gate, 2026-07-11:** This setup guide describes a
> future approved recovery. The Mac mini has no active CRE scheduler. Do not
> install, load, kickstart, or alter a launchd job until the operator runbook's
> explicit runtime-recovery and scheduler gates are approved.

A launchd user-agent cannot read files under `~/Documents` without a manual
Full Disk Access grant, so a clone there makes every scheduled run exit 126.
Two ways to avoid this:

- **Recommended:** clone outside `~/Documents` (for example `~/code/firecrawl`).
  No system grant needed; `cre_setup.sh` confirms you are clear.
- **Alternative:** keep it under `~/Documents` and grant Full Disk Access to
  `/bin/bash` (System Settings > Privacy & Security > Full Disk Access, use
  Cmd+Shift+G to type `/bin/bash`). This does not authorize scheduler loading.

---

## Mac mini production checklist

1. Clone outside `~/Documents`.
2. After runtime-recovery approval, `docker compose up -d api` from the repo
   root; `cre_setup.sh --check` uses only API readiness GETs. Run the separate
   `firecrawl_healthcheck.sh` only when a network scrape smoke is intended.
3. `export CRE_ENV_FILE=/path/to/EQUIRE/.env.local` (or place it at a default path).
4. `bash cre_setup.sh` and confirm `0 FAIL`.
5. Install schedules (gated, no load): `bash launchd/install_launchd.sh all`.
   If `CRE_ENV_FILE` is non-default, add `--env-file /path/.env.local` so it is
   baked into the jobs.
6. Stop here until the runbook's runtime-recovery and scheduler approval gates
   are explicitly approved. Do not load a tier.
7. Verify only with `bash cre_status.sh` and the other read-only runbook
   preflights. Do not use `launchctl kickstart` as a proof step.

---

## Development smoke on macOS or WSL2

Steps 1-4 work on both platforms. Ordinary setup already includes the default
bounded, no-database-write Interra probe. To run it separately after setup:

```bash
bash cre_setup.sh --check
npx tsx collect.ts --source=interra-realty --transaction=sale --max-items=3 --out=/tmp/interra-probe.json
python3 cre_ingest.py --in /tmp/interra-probe.json --dry-run
```

For a supervised high-volume refresh, do not invoke `cre_daily_update.sh`
directly. Apply the resource profile, recreate `api` and `playwright-service`,
then use the CPU-guarded checkpoint-series procedure in `START_HERE.md`.
Do **not** load launchd schedules on the dev machine; scheduling belongs on the
approved target host.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `exit 126`, `Operation not permitted` in `cre-*.err.log` | macOS TCC | relocate clone outside `~/Documents`, or grant `/bin/bash` Full Disk Access |
| `No POSTGRES_URL ... found` | env file not discovered | set `CRE_ENV_FILE` to the EQUIRE `.env.local` |
| `Firecrawl API readiness failed` | stack down, wrong service or port | verify `FIRECRAWL_API_URL` / `API_URL`; start the core with `docker compose up -d api` from repo root |
| smoke collect fails for one source | site layout drift | `CRE_SMOKE_SOURCE=<other> bash cre_setup.sh` |
| unexpected `docker context` | possibly wrong Docker backend | verify Docker Desktop WSL integration on Windows, or OrbStack on macOS; `default`, `desktop-linux` and `orbstack` are supported |
| a scheduled run was missed or failed | scheduler has not been approved or recovered | record the read-only preflight and follow the operator runbook |
| `cre_status.sh` flags a hung or stale lock | prior run or lock authority needs investigation | follow `START_HERE.md` Operational Recovery and its governed recovery tool; do not manually move or delete the lock or authority sidecar |

---

## Related docs

- `START_HERE.md` - live counts, per-source status, current run state
- `CLAUDE.md` - collector/ingestor reference
- `launchd/README.md` - tier gates, install, logs, portable-lock model
- `cre_status.sh` - read-only run-health heartbeat (schedules, staleness, last-run verdict, stack/env/TCC)
- `../../../docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md` - where to run the pipeline (decision aid)
- `firecrawl-ops` skill / `references/partner-orbstack-onboarding.md` - Firecrawl stack onboarding

## Supported execution environments

Run the CRE runtime with Linux Python/Node in Ubuntu WSL2, or on macOS.
Native Windows Python/Git Bash cannot provide the required POSIX file locks
and process groups. Runtime entrypoints refuse that platform with an actionable
message; the Python suite exits cleanly before importing POSIX-only modules.
Firecrawl's container API and agent helpers remain usable from PowerShell.

For WSL2, keep a separate clone and Linux dependencies under `~/code/firecrawl`
on ext4. Do not reuse Windows `node_modules`, copy production secrets or run a
second writer. See [Windows onboarding](../../../docs/firecrawl-ops/references/windows-docker-desktop-onboarding.md).
