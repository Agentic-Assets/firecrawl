# CRE Collector Setup

How to stand up the CRE listing pipeline on a fresh clone, on any Mac. This is
the runbook the coding agent on the Mac mini follows, and it is fully testable
here on the MacBook Pro first. One command (`cre_setup.sh`) does the checks and
the bootstrap; everything else is gated and explicit.

The pipeline has two halves:

- **Heavy half** (`collect.ts`, Node/tsx): scrapes brokerage sites through the
  local self-hosted **Firecrawl Docker stack** (API + headless Chromium + Redis
  + RabbitMQ + queue). Needs the stack up and a residential IP.
- **Light half** (`cre_ingest.py`, `cre_monitor.py`, `cre_gate.py`, Python
  stdlib only): upserts into Supabase `credeals` by shelling out to `psql`.
  Needs `psql` and a `POSTGRES_URL`.

---

## TL;DR (fresh clone)

```bash
# 1. Clone OUTSIDE ~/Documents (recommended on macOS; see "Scheduling" for why)
git clone <repo-url> ~/code/firecrawl
cd ~/code/firecrawl

# 2. Start the Firecrawl stack (OrbStack must be running)
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh

# 3. Point the ingestor at the EQUIRE database env (holds POSTGRES_URL*)
export CRE_ENV_FILE=/path/to/EQUIRE/.env.local

# 4. Bootstrap + verify the collector (installs deps, runs checks + smoke test)
cd scripts/firecrawl-ops/cre_collector
bash cre_setup.sh

# 5. (Optional, gated) render + install the launchd schedules (does NOT load)
bash launchd/install_launchd.sh all
```

Expect `cre_setup.sh` to finish with `0 FAIL`. Warnings are deploy-time gaps it
tells you how to close.

---

## Prerequisites

| Need | How | Notes |
|------|-----|-------|
| OrbStack (Docker) | https://orbstack.dev | `docker context show` must be `orbstack` |
| Node + npm | `brew install node` | `collect.ts` runs via `npx tsx` |
| libpq / psql | `brew install libpq` | ingestor auto-detects `/opt/homebrew/opt/libpq/bin/psql`; no PATH edit needed |
| python3 | preinstalled on macOS | stdlib only, zero pip deps |
| pytest (dev/CI only) | `python3 -m pip install pytest` | only to run `tests/`; the pipeline itself never needs it. `cre_setup.sh` reports it as a soft WARN, not a FAIL |
| Firecrawl stack | `docker compose up -d` from repo root | see `scripts/firecrawl-ops` `firecrawl-ops` skill or `references/partner-orbstack-onboarding.md` |
| EQUIRE `.env.local` | from the EQUIRE repo | holds `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL`; never committed here |

---

## `cre_setup.sh` (run this first)

```bash
bash cre_setup.sh              # full: toolchain + stack + npm install + code health + smoke
bash cre_setup.sh --check      # read-only doctor (no install, no network smoke)
bash cre_setup.sh --no-smoke   # install + checks, skip the network smoke test
bash cre_setup.sh --reinstall  # force npm install even if node_modules exists
```

It verifies the toolchain, OrbStack context, Firecrawl health, Node deps
(installing them if missing), TypeScript typecheck + Python compile, and
`POSTGRES_URL` discovery, then runs an offline smoke test (a tiny 3-item collect
through the stack, then `cre_ingest.py --dry-run`, which builds SQL and prints
stats without connecting to the database). It never writes to the live database
and never prints the database URL. Exit code is nonzero only if a hard
prerequisite (toolchain, deps, code health) fails.

Override the smoke source with `CRE_SMOKE_SOURCE=<key>` if a site's layout has
drifted (valid keys are listed in `collect.ts`).

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

---

## Scheduling (launchd, gated)

Schedules are macOS launchd jobs. The committed source of truth is the three
**`*.plist.template`** files; rendered per-machine plists are gitignored and
produced by the generator:

```bash
bash launchd/install_launchd.sh <monitor|daily|weekly|all>   # render + install, NO load
bash launchd/install_launchd.sh --load monitor daily         # also launchctl load -w (when gated OK)
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
| monitor | every 3h at :15 | cheap enumeration diff, observe-only (007 tables) | safe once stack + DB verified |
| daily | 06:30 daily | full collect + additive ingest (`--no-mark-missing`) | safe once stack is stable |
| weekly | Sun 03:00 | full collect + `--mark-missing` (ONLY tier that soft-deletes) | load ONLY after `cre_gate.py` proven on a Tier-1 source |

### macOS Full Disk Access (TCC)

A launchd user-agent cannot read files under `~/Documents` without a manual
Full Disk Access grant, so a clone there makes every scheduled run exit 126.
Two ways to avoid this:

- **Recommended:** clone outside `~/Documents` (for example `~/code/firecrawl`).
  No system grant needed; `cre_setup.sh` confirms you are clear.
- **Alternative:** keep it under `~/Documents` and grant Full Disk Access to
  `/bin/bash` (System Settings > Privacy & Security > Full Disk Access, use
  Cmd+Shift+G to type `/bin/bash`), then
  `launchctl kickstart -k gui/$(id -u)/ai.agentic.cre-daily`.

---

## Mac mini production checklist

1. Clone outside `~/Documents`.
2. `docker compose up -d` from the repo root, then `firecrawl_healthcheck.sh`.
3. `export CRE_ENV_FILE=/path/to/EQUIRE/.env.local` (or place it at a default path).
4. `bash cre_setup.sh` and confirm `0 FAIL`.
5. Install schedules (gated, no load): `bash launchd/install_launchd.sh all`.
   If `CRE_ENV_FILE` is non-default, add `--env-file /path/.env.local` so it is
   baked into the jobs.
6. Load the safe tiers when ready: `bash launchd/install_launchd.sh --load monitor daily`.
   Keep **weekly** unloaded until the gate is proven (see `launchd/README.md`).
7. Verify: `bash cre_status.sh` (read-only heartbeat: which tiers are loaded,
   last exit, staleness, last-run verdict, stack/env/TCC). Low-level alternative:
   `launchctl list | grep ai.agentic.cre` (a `0` in column 2 means the last run
   succeeded; `126` is the TCC block).
8. Prove one real run: `launchctl kickstart -k gui/$(id -u)/ai.agentic.cre-daily`,
   then check `out/daily/cre-daily.out.log` and re-run `bash cre_status.sh`.

---

## Testing here on the MacBook Pro (dev)

Steps 1-4 work identically. Run the pipeline manually to exercise it without
scheduling:

```bash
bash cre_setup.sh
bash cre_daily_update.sh --no-mark-missing   # full additive run, status activation OFF
```

Do **not** load launchd schedules on the dev machine; scheduling belongs on the
Mac mini.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `exit 126`, `Operation not permitted` in `cre-*.err.log` | macOS TCC | relocate clone outside `~/Documents`, or grant `/bin/bash` Full Disk Access |
| `No POSTGRES_URL ... found` | env file not discovered | set `CRE_ENV_FILE` to the EQUIRE `.env.local` |
| `Firecrawl stack not healthy` | stack down | `docker compose up -d` from repo root |
| smoke collect fails for one source | site layout drift | `CRE_SMOKE_SOURCE=<other> bash cre_setup.sh` |
| `docker context` not `orbstack` | wrong Docker backend | open OrbStack, `docker context use orbstack` |
| a scheduled run was missed or failed | launchd does not backfill a skipped fire | `launchctl kickstart -k gui/$(id -u)/ai.agentic.cre-daily`, then re-check `bash cre_status.sh` |
| `cre_status.sh` flags a hung or stale lock | crashed/stuck prior run left `out/daily/.cre.lock` | confirm no live run (`launchctl list \| grep ai.agentic.cre`), then `rm -rf out/daily/.cre.lock`. See `START_HERE.md` Operational Recovery |

---

## Related docs

- `START_HERE.md` - live counts, per-source status, current run state
- `CLAUDE.md` - collector/ingestor reference
- `launchd/README.md` - tier gates, install, logs, portable-lock model
- `cre_status.sh` - read-only run-health heartbeat (schedules, staleness, last-run verdict, stack/env/TCC)
- `../../../docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md` - where to run the pipeline (decision aid)
- `firecrawl-ops` skill / `references/partner-orbstack-onboarding.md` - Firecrawl stack onboarding
