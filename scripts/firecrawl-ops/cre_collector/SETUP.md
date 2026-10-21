# CRE Collector Setup

How to prepare the CRE listing pipeline on macOS or Ubuntu in WSL2. Linux
checkouts and dependencies belong on ext4, separate from the native Windows
checkout. `cre_setup.sh --check` checks prerequisites; setup success does not
authorize production collection, database writes, scheduling or writer cutover.

For optional run notifications, use [Slack setup](slack-setup.md). CRE webhook
and database files are private per-host configuration, separate from Docker's
root `.env`; never commit their contents.

The pipeline has two halves:

- **Heavy half** (`collect.ts`, Node/tsx): scrapes brokerage sites through the
  local self-hosted **Firecrawl Docker stack** (API + headless Chromium + Redis
  + RabbitMQ + queue). Needs the stack up for browser-backed adapters and
  source-specific network access. Some brokerage anti-bot systems reject
  particular egress IPs; a passing small probe does not certify every source.
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

### Experimental JLL HTML reconstruction

The experimental `sources/jll-direct.ts` helper is not used by ordinary JLL
collection. Its converter requires the generated `apps/api/native/index.js`
and matching platform addon from the existing API workspace native package.
Ordinary collector imports, typecheck and unit tests do not require this addon.
The experimental conversion fails clearly if it has not been built; it never
falls back to the older incomplete HTML mapper.

Build the existing native package only in a genuine isolated test clone using
the API workspace's pinned pnpm version and its native build prerequisites
(Rust/Cargo, linker and platform tools). Its package build command is
`pnpm --dir apps/api/native build` from the repository root after installing
the API workspace dependencies. This is an explicit developer prerequisite,
not part of `cre_setup.sh`, a new collector build system or authorization to
install anything into the production runtime. Do not copy an opaque addon
from a running service or share platform binaries between Windows, WSL and
macOS. A successful Linux build does not verify macOS or Windows native builds.

Native dependency resolution is not fully frozen upstream: the package's
Cargo lock is ignored and includes a Git dependency without a tracked revision
pin. Retain the resolved Cargo lock, exact Git dependency commit, source/tree,
toolchain, build command and generated loader/addon hashes with each QA result.
The pnpm lock alone is insufficient evidence of native reproducibility.

Run `npm run test:jll-native` from the isolated clone's collector directory
for the separate native integration suite. It requires that build and fails
if it is missing; normal unit tests remain independent. Saved rendered-page
reconstruction is parser evidence only. It does not establish direct HTTP
fidelity, new observation freshness, speed, source acceptance or deployment
readiness. Follow [AGENTIC-2898](https://linear.app/agenticassets/issue/AGENTIC-2898)
for the remaining transport and source gates.

Known limitation: the native image extractor returns no guaranteed DOM order.
If neither the native gallery nor the search/base row supplies a photo prefix,
the unchanged JLL source preserves that supplied order, so the primary photo
can vary. The explicit native suite checks anchored order and legacy input
preservation; it does not certify unanchored repeat-order parity. That failed
equivalence criterion remains open and blocks transport acceptance.

### Isolated verification

For checkout roles, branch-versus-runtime pins, safe promotion, and rollback boundaries, see
[Development and runtime checkouts](../../../docs/firecrawl-ops/references/development-and-runtime-checkouts.md).
Collector suites belong in an independent test clone, using WSL's ext4 filesystem on Windows
or the host filesystem on macOS, never the runtime checkout. Native launcher fixtures run on Windows.

For Python and TypeScript tests, create an isolated clone so lock discovery cannot reach a
runtime checkout, then use a venv (also avoids Ubuntu's system-pip restriction):

```bash
# From the repository root; choose a new destination outside the runtime clone.
git -c core.autocrlf=false clone --no-hardlinks --config core.autocrlf=false . ~/code/firecrawl-tests
cd ~/code/firecrawl-tests
python3 -m venv .venv
.venv/bin/python -m pip install pytest
cd scripts/firecrawl-ops/cre_collector
npm ci --ignore-scripts
# Python integration tests also execute Node/tsx in the browser sidecar.
# This version must match apps/playwright-service-ts/package.json.
(cd ../../../apps/playwright-service-ts && npx --yes pnpm@11.4.0 install --frozen-lockfile)
../../../.venv/bin/python -m pytest tests/ -q --maxfail=1
npm test
```

On Linux/WSL, set clone-local `core.autocrlf=false` before checkout as shown,
so a global Windows-style setting cannot create attribute-normalization changes
that the real clean-Git admission guard rejects. The root `.venv` is already
ignored; a nested collector `.venv` is not. Check `git status --short` after
installing prerequisites; it must be empty. No global Git change, source
normalization or hidden-file flag is needed.

Both suites run only in that standalone clone. The Node packages above are
test prerequisites, not additional production services. Recheck the sidecar's
`packageManager` pin after pulling a revision that changes it.
Before a full suite, verify `node --import tsx --eval 'void 0'` from the sidecar
directory; collector dependencies alone do not satisfy that import. Use
`--maxfail=1` during iteration so a failure exposes its traceback promptly;
a passing run still executes the entire suite. Preserve failed logs, fix the
confirmed cause, then rerun the failed tests before the full suite.
For concurrent suites, use a separate standalone clone per lane so test lock
and artifact state cannot collide. On Linux/WSL alongside a live runtime, use
the [identity-preserving network containment recipe](tests/CLAUDE.md) and enable
only that namespace's loopback; root-mapped test identities fail the legitimate
non-root capacity-approval checks.

For local receipt-to-observation ordering tests, stamp both events with the
shared `observationNow()` clock; compare their canonical timestamps strictly.
Use raw `Date.now()` for intentional stale/future wall-clock fixtures, not as
an independent ordering baseline for the UTC-anchor/monotonic observation clock.

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
Alternate `.cluster.` usernames and nonempty URI `options` startup parameters
are unsupported on this governed path; set query-local limits with `SET LOCAL`
inside the relevant transaction instead.
Changing connection endpoint or upgrading a historical pooled fingerprint
requires a fresh checkpoint generation, not an edited resume manifest.
See [Supabase connection guidance](https://supabase.com/docs/guides/database/connecting-to-postgres)
and [SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement).
For the diagnosed IPv6/TLS failure and verified repair, use the
[WSL Supabase troubleshooting guide](../../../docs/firecrawl-ops/references/wsl-supabase-connection-troubleshooting.md).

---

## Scheduling by platform

The shared checkpoint runner owns collection, validation and database writes on
both platforms. Only the scheduler and host adapter differ. Windows uses Task
Scheduler, PowerShell and the Ubuntu ext4 checkout; follow the
[Windows task installation and acceptance guide](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md#windows-task-scheduler-installation-and-acceptance).
Its jobs execute without an active coding chat. Registration alone does not prove
successful unattended operation: verify the actual trigger, settlement, database
readback and Slack delivery using that guide. Do not install launchd jobs on Windows.

### macOS launchd reference (gated)

The macOS **`*.plist.template`** files describe host-specific launchd jobs;
rendered per-machine plists are gitignored and produced by the generator:

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

> **Historical observation, 2026-07-11:** This setup guide describes a
> future approved recovery. The Mac mini had no active CRE scheduler at that
> inspection; recheck current host state before relying on it. Do not
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
directly. Verify the existing reviewed resource profile and readiness, then
follow the checkpoint-series procedure in `START_HERE.md` (the native wrapper
on Windows). Resource changes and container recreation are separate operator
actions, not a required step before every refresh.
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
| a scheduled run was missed or failed | disabled task, missed trigger, or a runner/resource/provider/database failure | inspect the actual scheduler history, launcher audit and checkpoint; follow `START_HERE.md` and [Slack setup](slack-setup.md) for notification checks |
| `cre_status.sh` flags a hung or stale lock | prior run or lock authority needs investigation | follow `START_HERE.md` Operational Recovery and its governed recovery tool; do not manually move or delete the lock or authority sidecar |

---

## Related docs

- `START_HERE.md` - runtime and source procedures; current execution evidence lives on AGENTIC-2902
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

## Portable pipeline configuration

The opt-in `--pipeline --source-workers 2` behavior is shared by macOS and Linux/WSL; serial defaults remain unchanged. Follow [Start Here](START_HERE.md#opt-in-stage-overlap) for finite source admissions and the audit/recovery boundary. Setup never activates this mode or changes an installed task.
