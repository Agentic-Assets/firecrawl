# CRE Launchd Schedule

Three flock-serialized launchd tiers that automate the CRE listing pipeline.
All tiers are authored here and gated: **do not load any plist until the
prerequisite listed for that tier is satisfied.**

---

## Tiers

### monitor — `ai.agentic.cre-monitor.plist`

Runs at minute :15 of every third hour (00:15, 03:15, 06:15, 09:15, 12:15,
15:15, 18:15, 21:15 local time).

What it does: invokes `cre_run_tier.sh monitor`, which runs the incremental
enumeration-diff pipeline (`cre_monitor.py`). The monitor performs cheap
source enumeration, diffs `(external_id, status, price, lastmod)` against
`cre_source_index`, and enqueues only new or changed ids for detail enrichment
(Tier-B sitemap sources) or records change events directly from the feed
(Tier-A bulk-API sources). It does NOT run the full collect; it never calls
`--mark-missing`. New-listing latency for Tier-1 sources drops from ~24h to
the monitor interval.

Gate: `cre_monitor.py` and `cre_gate.py` both exist and are unit-tested.
`cre_run_tier.sh monitor` now runs `collect.ts --monitor` (cheap enumeration)
then `cre_monitor.py --in <artifact>` in observe-only mode. Passing `--apply`
requires setting `CRE_MONITOR_APPLY=1` in the environment.
**Loading this plist and enabling `--apply` (via `CRE_MONITOR_APPLY=1`) remain
GATED: do not load this plist or enable --apply without explicit go-ahead.**

---

### daily — `ai.agentic.cre-daily.plist`

Runs at 06:30 local time every day.

What it does: invokes `cre_run_tier.sh daily`, which calls
`cre_daily_update.sh --no-mark-missing`. This is the full collect-plus-ingest
cycle (all 15 sources, sale and lease, unlimited pagination) run additively:
rows are upserted and updated but no rows are soft-deleted. The daily run is
the reference pass for Tier-A change-event emission once the event ledger is
live.

Gate: safe to load once the daily script is stable and the stack is healthy.
The shared flock guarantees it will not overlap with an active monitor or
weekly run.

---

### weekly — `ai.agentic.cre-weekly.plist`

Runs Sunday at 03:00 local time.

What it does: invokes `cre_run_tier.sh weekly`, which calls
`cre_daily_update.sh --mark-missing`. This is the completeness backstop and
the ONLY tier permitted to soft-delete (disappear) rows.

CRITICAL CONSTRAINT: `--mark-missing` will soft-delete rows that a full,
clean run did not see. A partial or error-prone run with `--mark-missing`
active can silently remove live listings. Therefore:
- This tier must only be loaded after `cre_gate.py` is live and gating
  mark-missing eligibility per source (design-doc section 9, Phase 1).
- The runner must be proven on at least one Tier-1 source with a clean
  convergence record before the weekly plist is loaded.
- The gate must be prefix-aware (scope soft-delete by brokerage_id and
  external_id prefix) so a partial colliers-main run does not delete
  colliers SalesTracker rows or vice versa.

---

## Shared-flock guarantee

All three tiers acquire the same exclusive lock before doing any work:

```
LOCKFILE=/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock
exec 9>"$LOCKFILE"
flock -n 9 || exit 0
```

If a tier is already running the competing tier exits immediately and silently
(exit 0). launchd sees a clean exit and will not retry until the next
scheduled interval. This prevents the daily and monitor tiers from overlapping
mid-run, and prevents the weekly `--mark-missing` pass from starting while an
additive daily run is still in progress.

Manual runs of `cre_daily_update.sh` or `cre_run_tier.sh` from the terminal
also acquire the same lock (they call `cre_run_tier.sh` under the hood or can
be wrapped with the same flock command), so scheduled and ad-hoc runs
serialize correctly.

---

## Install and uninstall (gated)

Read the gate conditions above before loading any plist. When the prerequisites
are met, install by copying to `~/Library/LaunchAgents/` and loading:

```bash
# monitor (load only after Phase 3 + Tier-1 source proven)
cp /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector/launchd/ai.agentic.cre-monitor.plist \
   ~/Library/LaunchAgents/ai.agentic.cre-monitor.plist
launchctl load -w ~/Library/LaunchAgents/ai.agentic.cre-monitor.plist

# daily (safe once stack is stable)
cp /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector/launchd/ai.agentic.cre-daily.plist \
   ~/Library/LaunchAgents/ai.agentic.cre-daily.plist
launchctl load -w ~/Library/LaunchAgents/ai.agentic.cre-daily.plist

# weekly (load ONLY after cre_gate.py is live and proven on a Tier-1 source)
cp /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector/launchd/ai.agentic.cre-weekly.plist \
   ~/Library/LaunchAgents/ai.agentic.cre-weekly.plist
launchctl load -w ~/Library/LaunchAgents/ai.agentic.cre-weekly.plist
```

Uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/ai.agentic.cre-monitor.plist
launchctl unload ~/Library/LaunchAgents/ai.agentic.cre-daily.plist
launchctl unload ~/Library/LaunchAgents/ai.agentic.cre-weekly.plist
rm ~/Library/LaunchAgents/ai.agentic.cre-monitor.plist
rm ~/Library/LaunchAgents/ai.agentic.cre-daily.plist
rm ~/Library/LaunchAgents/ai.agentic.cre-weekly.plist
```

Verify a loaded job:

```bash
launchctl list | grep ai.agentic.cre
# A non-zero PID in column 1 means the job is running now.
# Exit code 0 in column 2 means the last run succeeded.
```

---

## Logs

All tiers write stdout and stderr into `out/daily/` beside the existing daily
run logs:

| tier    | stdout                        | stderr                        |
|---------|-------------------------------|-------------------------------|
| monitor | `out/daily/cre-monitor.out.log` | `out/daily/cre-monitor.err.log` |
| daily   | `out/daily/cre-daily.out.log`   | `out/daily/cre-daily.err.log`   |
| weekly  | `out/daily/cre-weekly.out.log`  | `out/daily/cre-weekly.err.log`  |

Each run also emits a timestamped START/END line via `cre_run_tier.sh` so you
can grep for the exact wall-clock boundaries of any tier run.
