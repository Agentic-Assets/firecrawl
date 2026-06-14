# launchd Module

## Most Critical Rule

**Do not `launchctl load` any plist until that tier's gate in `README.md` is satisfied.** Weekly is the only tier that may pass `--mark-missing` (soft-delete rows). Load weekly only after `cre_gate.py` is wired into `cre_daily_update.sh` and proven on at least one Tier-1 source with prefix-aware scope.

Current state (2026-06-14): monitor and daily tiers are LOADED but BLOCKED. Every scheduled fire exits 126 because the repo lives under `~/Documents` (TCC) and the launchd user-agent lacks macOS Full Disk Access; no scheduled run has succeeded yet. One-time fix (Full Disk Access grant to `/bin/bash`) is in `../START_HERE.md` Known Limits. Weekly is intentionally NOT loaded (held for explicit go-ahead).

## Folder-Specific Commands

```bash
bash launchd/cre_run_tier.sh {monitor|daily|weekly}   # manual; same flock as plists
launchctl list | grep ai.agentic.cre                  # PID col 1 = running
```

Install/unload: `README.md` (copy to `~/Library/LaunchAgents/`, `launchctl load -w` / `unload`).

## Naming Patterns

- Labels: `ai.agentic.cre-{monitor|daily|weekly}` (not parent `com.agenticassets.cre-daily.example`).
- Dispatcher: `cre_run_tier.sh <tier>`; logs under `../out/daily/cre-{tier}.{out,err}.log`.
- Lock: `../out/daily/.cre.lock` - held tier wins; competitor exits 0 silently.

## Module Boundaries

Owns macOS schedules, flock serialization, tier dispatch. Delegates collect/ingest to `cre_daily_update.sh` (daily/weekly). Monitor tier: `collect.ts --monitor` (enumeration artifact) then `cre_monitor.py` (observe-only diff; `CRE_MONITOR_APPLY=1` for `--apply`). Plists hardcode this Mac's absolute repo path; update paths if the clone moves.

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
