# launchd Module

## Most Critical Rule

**Do not `launchctl load` any plist until that tier's gate in `README.md` is satisfied.** Weekly is the only tier that may pass `--mark-missing` (soft-delete rows). Load weekly only after `cre_gate.py` is wired into `cre_daily_update.sh` and proven on at least one Tier-1 source with prefix-aware scope.

Current state (2026-06-14): monitor and daily tiers are LOADED but BLOCKED. Every scheduled fire exits 126 because the repo lives under `~/Documents` (TCC) and the launchd user-agent lacks macOS Full Disk Access; no scheduled run has succeeded yet. One-time fix (Full Disk Access grant to `/bin/bash`) is in `../START_HERE.md` Known Limits. Weekly is intentionally NOT loaded (held for explicit go-ahead).

## Folder-Specific Commands

```bash
bash launchd/install_launchd.sh all                   # render + install plists (gated, NO load)
bash launchd/install_launchd.sh --load monitor daily  # install + load (when gate met)
bash launchd/cre_run_tier.sh {monitor|daily|weekly}   # manual; same portable lock as plists
bash cre_status.sh                                    # read-only run-health heartbeat (preferred)
launchctl list | grep ai.agentic.cre                  # PID col 1 = running; col 2 = last exit (ephemeral)
```

Install/unload: `README.md`. `install_launchd.sh` renders `*.plist.template` per-machine, validates with `plutil`, installs to `~/Library/LaunchAgents/`; `--load` to load, `--uninstall` to remove.

## Naming Patterns

- Labels: `ai.agentic.cre-{monitor|daily|weekly}` (not parent `com.agenticassets.cre-daily.example`).
- Dispatcher: `cre_run_tier.sh <tier>`; logs under `../out/daily/cre-{tier}.{out,err}.log`.
- Lock: `../out/daily/.cre.lock` - held tier wins; competitor exits 0 silently.

## Module Boundaries

Owns macOS schedules, lock serialization (portable atomic `mkdir` lock with PID-based stale recovery; no `flock` dependency, since stock macOS ships none), tier dispatch, and a per-run verdict marker (`out/daily/last_run_<tier>.json`). Delegates collect/ingest to `cre_daily_update.sh` (daily/weekly). Monitor tier: `collect.ts --monitor` (enumeration artifact) then `cre_monitor.py` (observe-only diff; `CRE_MONITOR_APPLY=1` for `--apply`), with both children redirected to a per-run, pruned `out/monitor/monitor_<stamp>.log` (not the append-only launchd redirect, which fires 8x/day). Plists are rendered per-machine from `*.plist.template` by `install_launchd.sh` (tokens for collector path, PATH, optional `CRE_ENV_FILE`); `cre_run_tier.sh` self-locates, so no committed file hardcodes a clone path.

**Disk self-bounds on every run.** `finish()` (EXIT trap, pass or fail) prunes runtime artifacts: keep newest 24 `monitor_*.json` + 24 `monitor_*.log` under `out/monitor/`, and cap each `cre-*.{out,err}.log` at 10MB (`_keep_newest` / `_cap_log`, both space-safe, BSD/GNU `stat` fallback). The lock owner records `<pid> <start-epoch>` so `cre_status.sh` can flag a hung lock (held beyond any real run) or a stale lock (dead PID). No cron cleanup needed.

## Integration Points

| Tier | Schedule | Downstream |
|------|----------|------------|
| monitor | :15 every 3h | `collect.ts --monitor` → `cre_monitor.py` |
| daily | 06:30 | `cre_daily_update.sh --no-mark-missing` |
| weekly | Sun 03:00 | `cre_daily_update.sh --mark-missing` |

## References

- `../CLAUDE.md` - ingest, mark-missing guards, daily script defaults
- `README.md` - gates, install, logs
- `../START_HERE.md` - gate/monitor wiring status and the launchd TCC blocker (Known Limits)
- `../../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` - section 9, 14.4 step 6
- `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
