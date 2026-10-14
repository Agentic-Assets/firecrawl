# launchd Module

## Most Critical Rule

**Do not `launchctl load` any plist until gate 5 in the 2026-07-11 operator runbook records Cayman's explicit approval of the named coordinator, owner, exact labels, credentials, observation window, and rollback.** Weekly is the only tier that may pass `--mark-missing` (soft-delete rows), and only under the separate `CRE_WEEKLY_MARK_MISSING=1` escalation; by default it runs additive (`--no-mark-missing`). The `--mark-missing` escalation stays held until `cre_gate.py` is proven on at least one Tier-1 source with prefix-aware scope (it already wires into `cre_daily_update.sh` as observe-only step [3/4]). GitHub Actions remains manual-only, and aa-hub is not an execution control plane.

**CPU-safety gate.** The templates call legacy tier workers; weekly and daily
use the legacy full backstop. They do not invoke the supervised guarded runners
and do not have their equivalent host-CPU guard
or resource profile, so no tier is eligible to load until equivalent protection
is designed, tested, and approved.

Tier set (cadence restructure SHIPPED in code 2026-06-15; live cutover gated): **monitor** (2x/day), **enrich** (every 4h, drains `cre_enrichment_queue`), **weekly** (additive full backstop). The heavy **daily** tier is RETIRED (monitor + enrich replace its freshness role); its case + template are kept for rollback only. Design + cutover runbook: `../ENRICHMENT_WORKER_DESIGN_2026-06-15.md` Section 9.

Historical state captured on 2026-06-15: the old monitor and daily tiers were
loaded on that Mac, and the checkout was outside `~/Documents`. This is not
current scheduler evidence. The 2026-07-11 read-only audit at
`../../../../tasks/2026-07-10-cre-consolidation-review/2026-07-11-execution-status-audit.md`
records a historical point-in-time Mac mini state and recovery gates. Re-run
`cre_status.sh`
before any scheduler decision. The new enrich/restructured-monitor/weekly
cutover remains held for explicit approval.

## Folder-Specific Commands

```bash
bash launchd/install_launchd.sh all                      # render + install monitor/enrich/weekly (NO load)
bash launchd/install_launchd.sh --load monitor enrich     # only after recorded gate-5 approval
bash launchd/cre_run_tier.sh {monitor|enrich|weekly|daily} # manual; same governed Python authority as plists (daily = retired rollback case)
bash cre_status.sh                                        # read-only run-health heartbeat (preferred)
bash cre_status.sh --expected-sha <merged-sha>             # exact clean deployment identity
bash cre_status.sh --scheduler external                   # Linux/WSL: artifacts, lock, API and checkout
launchctl list | grep ai.agentic.cre                      # PID col 1 = running; col 2 = last exit (ephemeral)
```

Install/unload: `README.md`. `install_launchd.sh` renders `*.plist.template` per-machine, validates with `plutil`, installs to `~/Library/LaunchAgents/`; `--load` to load, `--uninstall` to remove.

## External visible-surface contract (Linux/WSL)

`cre_status.sh --scheduler external` is read-only. It skips launchctl, installed
plists, macOS TCC and launchd stderr checks, while retaining checkout identity,
environment discovery, API readiness, lock ownership, run verdicts and artifact
staleness. It checks monitor, enrich and weekly against their existing freshness
thresholds; daily remains retired. A fresh clone with no artifacts is reported as
never run, not as proof of an active schedule. External activation and scheduler
exit status must be inspected in the approved visible surface. Status exits `0`
when no problems are observed, `1` for WARN/FAIL findings, and `2` for invalid
arguments. Pass `CRE_ENV_FILE` explicitly; external mode never discovers it from
an installed Mac plist.

The existing scheduler-independent tier entrypoint is:

```powershell
# Invocation contract only. Do not schedule or run until the gates below pass.
wsl.exe -d Ubuntu-24.04 --cd /home/<user>/code/firecrawl/scripts/firecrawl-ops/cre_collector -- env CRE_ENV_FILE=/home/<user>/.config/cre/equire.env python3 cre_tier_dispatch.py monitor
```

Use the approved tier (`monitor`, `enrich` or `weekly`); never the retired daily
tier for a new schedule. This is an execution command, **not a dry run**. The
dispatcher has no dry-run CLI. `python3 cre_tier_dispatch.py --help` is a safe
syntax check; isolated tests with a synthetic worker and private lock verify
dispatch behavior without collection or database access.

The dispatcher holds the canonical `SharedLock` authority and recovery-sync
flocks until its owned worker and descendants finish. A competing lock holder
causes a documented skip with exit `0`, so exit `0` alone does not prove a run or
freshness. Otherwise the dispatcher propagates the worker exit code; an owned
termination signal is represented as `128 + signal`, argument errors exit `2`,
and dispatcher authority errors exit `1`. Inspect run markers and artifacts
alongside the visible task result. Never remove the canonical lock manually.

The lock is local and cannot serialize two computers. Freeze and verify the Mac
writer before any Windows production execution, then record the sole writer on
the live refresh issue. Production canary, generation-exact readback, credentials,
host approval, telemetry protection and rollback gates remain mandatory. These
legacy tiers still lack the checkpoint-series resource protection, so implementing
Linux telemetry alone does not make a scheduled tier eligible to run. A named
visible surface and its registry approval are required before recurring execution.
This contract creates no Codex automation, Task Scheduler entry, cron job, systemd
timer or launchd job.

## Naming Patterns

- Labels: `ai.agentic.cre-{monitor|enrich|weekly}` (plus the retired `ai.agentic.cre-daily`, rollback-only; not parent `com.agenticassets.cre-daily.example`).
- Dispatcher: `cre_run_tier.sh <tier>`; logs under `../out/daily/cre-{tier}.{out,err}.log`.
- Lock: `../out/daily/.cre.lock` - held tier wins; competitor exits 0 silently.

## Module Boundaries

Owns macOS schedules, tier dispatch, and a per-run verdict marker (`out/daily/last_run_<tier>.json`). `cre_tier_dispatch.py` owns serialization through the canonical Python `SharedLock`, persistent authority, and recovery-sync flock. It places the worker in its own session/process group, forwards termination only while that unreaped leader proves ownership, and keeps authority through descendant reaping without signaling a post-reap numeric PGID. `cre_run_tier.sh` is an inherited-descriptor-verified worker and must never create, reclaim, or remove `.cre.lock` itself. Delegates collect/ingest to `cre_daily_update.sh` (weekly) and to `cre_enrich.py` (enrich). Monitor tier: `collect.ts --monitor` (enumeration artifact) then `cre_monitor.py` (observe-only diff; `CRE_MONITOR_APPLY=1` for `--apply`), with both children redirected to a per-run, pruned `out/monitor/monitor_<stamp>.log` (not the append-only launchd redirect). Enrich tier: `cre_enrich.py --batch ${CRE_ENRICH_BATCH:-200}`, additive by construction (`cre_ingest.py --in` only; never `--mark-missing`/`--activate-status`). Plists are rendered per-machine from `*.plist.template` by `install_launchd.sh` (tokens for collector path, PATH, optional `CRE_ENV_FILE`, and optional owner-only alert secret-file path); `cre_run_tier.sh` self-locates, so no committed file hardcodes a clone path.

**Disk self-bounds on every run.** `finish()` (EXIT trap, pass or fail) prunes runtime artifacts: keep newest 24 `monitor_*.json` + 24 `monitor_*.log` under `out/monitor/`, and cap each `cre-*.{out,err}.log` at 10MB (`_keep_newest` / `_cap_log`, both space-safe, BSD/GNU `stat` fallback). The lock owner records `<pid> <start-epoch>` so `cre_status.sh` can flag a hung lock (held beyond any real run) or a stale lock (dead PID). No cron cleanup needed.

## Integration Points

| Tier | Schedule | Downstream |
|------|----------|------------|
| monitor | 06:10, 18:10 (2x/day) | `collect.ts --monitor` → `cre_monitor.py` (enqueues new/changed) |
| enrich | every 4h (00:30/04:30/08:30/12:30/16:30/20:30) | `cre_enrich.py` → `collect.ts --enrich-input` → `cre_ingest.py --in` (additive; never `--mark-missing`/`--activate-status`) |
| weekly | Sun 03:00 | `cre_daily_update.sh --no-mark-missing` (additive backstop); `--mark-missing` only under `CRE_WEEKLY_MARK_MISSING=1` (gated) |
| ~~daily~~ | retired | `cre_daily_update.sh --no-mark-missing` (rollback case only; no longer scheduled) |

## References

- `../CLAUDE.md` - ingest, mark-missing guards, daily script defaults
- `README.md` - gates, install, logs
- `../START_HERE.md` - gate/monitor wiring status and launchd run-health (Known Limits)
- `../../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` - section 9, 14.4 step 6
- `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
