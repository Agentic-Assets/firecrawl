# launchd Module

## Most Critical Rule

**Do not `launchctl load` any plist until gate 5 in the 2026-07-11 operator runbook records Cayman's explicit approval of the named coordinator, owner, exact labels, credentials, observation window, and rollback.** Weekly is the only tier that may pass `--mark-missing` (soft-delete rows), and only under the separate `CRE_WEEKLY_MARK_MISSING=1` escalation; by default it runs additive (`--no-mark-missing`). The `--mark-missing` escalation stays held until `cre_gate.py` is proven on at least one Tier-1 source with prefix-aware scope (it already wires into `cre_daily_update.sh` as observe-only step [3/4]). GitHub Actions remains manual-only, and aa-hub is not an execution control plane.

Tier set (cadence restructure SHIPPED in code 2026-06-15; live cutover gated): **monitor** (2x/day), **enrich** (every 4h, drains `cre_enrichment_queue`), **weekly** (additive full backstop). The heavy **daily** tier is RETIRED (monitor + enrich replace its freshness role); its case + template are kept for rollback only. Design + cutover runbook: `../ENRICHMENT_WORKER_DESIGN_2026-06-15.md` Section 9.

Historical state captured on 2026-06-15: the old monitor and daily tiers were
loaded on that Mac, and the checkout was outside `~/Documents`. This is not
current scheduler evidence. The 2026-07-11 read-only audit at
`../../../../tasks/2026-07-10-cre-consolidation-review/2026-07-11-execution-status-audit.md`
records the actual Mac mini state and recovery gates. Re-run `cre_status.sh`
before any scheduler decision. The new enrich/restructured-monitor/weekly
cutover remains held for explicit approval.

## Folder-Specific Commands

```bash
bash launchd/install_launchd.sh all                      # render + install monitor/enrich/weekly (NO load)
bash launchd/install_launchd.sh --load monitor enrich     # only after recorded gate-5 approval
bash launchd/cre_run_tier.sh {monitor|enrich|weekly|daily} # manual; same portable lock as plists (daily = retired rollback case)
bash cre_status.sh                                        # read-only run-health heartbeat (preferred)
bash cre_status.sh --expected-sha <merged-sha>             # exact clean deployment identity
launchctl list | grep ai.agentic.cre                      # PID col 1 = running; col 2 = last exit (ephemeral)
```

Install/unload: `README.md`. `install_launchd.sh` renders `*.plist.template` per-machine, validates with `plutil`, installs to `~/Library/LaunchAgents/`; `--load` to load, `--uninstall` to remove.

## Naming Patterns

- Labels: `ai.agentic.cre-{monitor|enrich|weekly}` (plus the retired `ai.agentic.cre-daily`, rollback-only; not parent `com.agenticassets.cre-daily.example`).
- Dispatcher: `cre_run_tier.sh <tier>`; logs under `../out/daily/cre-{tier}.{out,err}.log`.
- Lock: `../out/daily/.cre.lock` - held tier wins; competitor exits 0 silently.

## Module Boundaries

Owns macOS schedules, lock serialization (portable atomic `mkdir` lock with PID-based stale recovery; no `flock` dependency, since stock macOS ships none), tier dispatch, and a per-run verdict marker (`out/daily/last_run_<tier>.json`). Delegates collect/ingest to `cre_daily_update.sh` (weekly) and to `cre_enrich.py` (enrich). Monitor tier: `collect.ts --monitor` (enumeration artifact) then `cre_monitor.py` (observe-only diff; `CRE_MONITOR_APPLY=1` for `--apply`), with both children redirected to a per-run, pruned `out/monitor/monitor_<stamp>.log` (not the append-only launchd redirect). Enrich tier: `cre_enrich.py --batch ${CRE_ENRICH_BATCH:-200}`, additive by construction (`cre_ingest.py --in` only; never `--mark-missing`/`--activate-status`). Plists are rendered per-machine from `*.plist.template` by `install_launchd.sh` (tokens for collector path, PATH, optional `CRE_ENV_FILE`, and optional owner-only alert secret-file path); `cre_run_tier.sh` self-locates, so no committed file hardcodes a clone path.

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
