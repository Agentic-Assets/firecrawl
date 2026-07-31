# CRE Launchd Schedule

Lock-serialized launchd tiers that automate the CRE listing pipeline. All tiers
are authored here and gated: **do not load any plist until the prerequisite
listed for that tier is satisfied.**

Tier set (cadence restructure SHIPPED in code 2026-06-15):

- **monitor** (2x/day, 06:10 / 18:10): cheap enumeration diff; enqueues
  new/changed listings into `cre_enrichment_queue`.
- **enrich** (every 4h, 00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30): drains
  that queue, scrapes ONLY the flagged listings' detail, re-ingests additively.
- **weekly** (Sun 03:00): full collect + ingest backstop; additive by default
  (`--no-mark-missing`). It also refreshes detail and recovers dead-lettered
  queue rows. Additive semantics reduce data risk but do not authorize loading.
- **daily**: RETIRED. monitor (2x/day) + enrich (every 4h) replace its freshness
  role at a fraction of the cost. The template + dispatcher case are kept for
  rollback only.

Full design + the gated cutover runbook:
`../ENRICHMENT_WORKER_DESIGN_2026-06-15.md` (Section 2 tier model, Section 9
cutover).

> Historical state captured on 2026-06-15: the OLD tiers were loaded on that Mac
> (`ai.agentic.cre-monitor` every 3h + `ai.agentic.cre-daily` 06:30), both
> EXECUTING on schedule. The repo was relocated out of `~/Documents` to
> `~/Github/agentic-assets/firecrawl`, so the launchd user-agent no longer needs
> macOS Full Disk Access and scheduled fires no longer exit 126. The monitor
> tier has a confirmed clean run (`../out/daily/last_run_monitor.json` rc:0,
> 2026-06-15). The restructured monitor (2x/day), the new enrich tier, and the
> additive weekly backstop are installable but NOT yet loaded; running the
> Section 9 cutover is held for explicit go-ahead. On a fresh machine, keep the
> clone outside `~/Documents` so TCC never applies (`../SETUP.md`).
>
> Do not use this historical snapshot as current scheduler evidence. The
> 2026-07-11 read-only audit in
> `../../../../tasks/2026-07-10-cre-consolidation-review/2026-07-11-execution-status-audit.md`
> records a historical point-in-time Mac mini state and required recovery
> gates. Re-run
> `../cre_status.sh` before any scheduler decision.
>
> Setup is portable: render + install plists per-machine with
> `install_launchd.sh` (see below). The bare `ai.agentic.cre-*.plist` on this
> Mac are gitignored local artifacts; the committed source is the templates.

---

## Tiers

### monitor - `ai.agentic.cre-monitor.plist`

Runs twice daily at 06:10 and 18:10 local time (restructured from the prior
every-3h cadence; the enrich tier now carries the detail-refresh load between
monitor passes).

What it does: invokes `cre_run_tier.sh monitor`, which runs the incremental
enumeration-diff pipeline (`cre_monitor.py`). The monitor performs cheap
source enumeration, diffs `(external_id, status, price, lastmod)` against
`cre_source_index`, and enqueues only new or changed ids into
`cre_enrichment_queue` for the enrich tier to drain (Tier-B sitemap sources) or
records change events directly from the feed (Tier-A bulk-API sources). It does
NOT run the full collect; it never calls `--mark-missing`.

Gate: `cre_monitor.py` and `cre_gate.py` both exist and are unit-tested.
`cre_run_tier.sh monitor` runs `collect.ts --monitor` (cheap enumeration) then
`cre_monitor.py --in <artifact>` in observe-only mode. Passing `--apply`
requires setting `CRE_MONITOR_APPLY=1` in the environment.
**Historical status (2026-06-15): the monitor plist was loaded with
`CRE_MONITOR_APPLY=1` and had a successful run at the old every-3h cadence.
This is not current scheduler evidence. Re-run `../cre_status.sh` and consult
the dated execution-status audit before loading or changing any tier.**

---

### enrich - `ai.agentic.cre-enrich.plist` (NEW)

Runs every 4h at 00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30 local time
(offset 30 min from the monitor so the two never collide; the shared lock is the
backstop if they ever do).

What it does: invokes `cre_run_tier.sh enrich`, which runs
`cre_enrich.py --batch ${CRE_ENRICH_BATCH:-200}`. The worker claims a batch from
`cre_enrichment_queue` (the rows the monitor flagged new/changed), runs
`collect.ts --enrich-input` to render ONLY those listings' detail pages, then
re-ingests the result additively via `cre_ingest.py --in` and deletes the rows it
completed so a later change to the same listing can re-enqueue. This closes the
loop the monitor's enqueue path always fed and replaces the nightly full
re-render of every listing.

For a manual recovery of one backed-up broker, an operator may run
`python3 cre_enrich.py --source SOURCE_KEY --batch N`. `SOURCE_KEY` is an exact
queue `source_key` filter, applied inside the locked claim query: the invocation
cannot claim, retry, or increment attempts for another source. It is an ad hoc
operator command only; the scheduled enrich tier remains unfiltered.

Additive by construction: the worker ALWAYS ingests with `--in` only and NEVER
passes `--mark-missing` or `--activate-status`, so it cannot soft-delete or flip
board state. A whole-run failure (collect rc != 0, or `enriched.json` missing /
invalid / empty) releases the claims without ingesting and exits nonzero; a
crashed run's claims are reclaimed after 1h. Dead-lettered rows (attempts >= 5)
surface in `v_cre_enrichment_dead` and ride the weekly additive backstop.

Technical prerequisites: `cre_enrich.py` is unit-tested and `010` is applied
(the queue-health views). The shared lock prevents overlap with monitor or
weekly. Loading still requires the separate scheduler-activation gate in the
operator runbook.

---

### weekly - `ai.agentic.cre-weekly.plist`

Runs Sunday at 03:00 local time.

What it does: invokes `cre_run_tier.sh weekly`, which calls
`cre_daily_update.sh` with a conditional mark-missing flag. It is the full
collect-plus-ingest completeness + detail-refresh + dead-letter-recovery backstop.

ADDITIVE BY DEFAULT: with `CRE_WEEKLY_MARK_MISSING` unset, the dispatcher passes
`--no-mark-missing`, so the weekly tier upserts and refreshes but never
soft-deletes. That makes its data behavior additive, but it does not authorize
loading. The separate scheduler-activation gate still applies.

`--mark-missing` is a SEPARATE, GATED escalation: it fires only when
`CRE_WEEKLY_MARK_MISSING=1` is set in this tier's environment. weekly is still
the ONLY tier permitted to soft-delete (disappear) rows, and even when escalated
the soft-delete stays triple-gated (dispatcher branch + `cre_gate.py --strict`
auto-downgrade + per-brokerage ingest eligibility). Therefore the escalation
must only be enabled after:
- `cre_gate.py` is live and gating mark-missing eligibility per source
  (design-doc Section 2.1); it is now wired into `cre_daily_update.sh` as
  observe-only step [3/4].
- The runner is proven on at least one Tier-1 source with a clean convergence
  record.
- The gate is prefix-aware (scope soft-delete by brokerage_id and external_id
  prefix) so a partial colliers-main run does not delete colliers SalesTracker
  rows or vice versa.

---

### daily - `ai.agentic.cre-daily.plist` (RETIRED)

The heavy daily full re-scrape is retired: monitor (2x/day) + enrich (every 4h)
replace its freshness role at a fraction of the cost. The template and the
`cre_run_tier.sh daily` case (`cre_daily_update.sh --no-mark-missing`) are kept
for rollback only and are no longer scheduled. The Section 9 cutover unloads the
live daily plist.

---

## Shared-lock guarantee

Every tier acquires the same exclusive lock before doing any work. The lock
is a portable atomic `mkdir` (no `flock` dependency, since stock macOS does not
ship flock) with PID-based stale-lock recovery:

```
# COLLECTOR_DIR is self-located by cre_run_tier.sh (launchd/..); no hardcoded path.
LOCKDIR="${COLLECTOR_DIR}/out/daily/.cre.lock"   # a directory, not a file
mkdir "$LOCKDIR" 2>/dev/null || { still-alive owner? exit 0 : reclaim stale }
trap 'rm -rf "$LOCKDIR"' EXIT                     # released on any exit
```

If a tier is already running (the lock dir exists and its recorded PID is still
alive) the competing tier exits immediately and silently (exit 0). launchd sees
a clean exit and will not retry until the next scheduled interval. A lock left
by a crashed run (PID no longer alive) is reclaimed automatically. This prevents
the monitor, enrich, and weekly tiers from overlapping mid-run, and prevents the
weekly pass from starting while an enrich or monitor run is in progress.

> History: the lock used to be `flock -n 9`. Because macOS ships no `flock`, the
> missing binary returned 127 and was misread as "lock held", so every scheduled
> tier exited 0 without doing any work. The mkdir lock removes that dependency.

Manual runs of `cre_run_tier.sh` from the terminal acquire the same lock, so
scheduled and ad-hoc runs serialize correctly. (Running `cre_daily_update.sh`
directly does not take the lock; prefer `cre_run_tier.sh weekly` for a locked,
marker-writing full run.)

The lock owner records `<pid> <start-epoch>` in `${LOCKDIR}/pid`, so
`cre_status.sh` can tell three states apart: no lock held; a live lock held
beyond any legitimate run length (flagged "possible hung run"); or a stale lock
whose recorded PID is no longer alive (auto-reclaimed by the next tier, but
surfaced so you are not surprised). To clear a wedged lock by hand, see
`../START_HERE.md` Operational Recovery.

---

## Install and uninstall (gated)

Plists are portable: the committed source of truth is the
**`*.plist.template`** files (path-agnostic tokens), and `install_launchd.sh`
renders a machine-specific copy, validates it with `plutil`, and installs it.
`install_launchd.sh all` covers the active set (monitor, enrich, weekly); the
retired daily template installs only if named explicitly. Rendered
`ai.agentic.cre-*.plist` files are gitignored. The runner (`cre_run_tier.sh`)
self-locates, so nothing here hardcodes a clone path.

Read the gate conditions above before loading any tier. **Rendering and
installing never loads a job; loading requires `--load`.**

```bash
# Render + install the active set monitor/enrich/weekly (NO load), safe anywhere:
bash install_launchd.sh all

# If the EQUIRE .env.local is not at a default ~/Documents path, bake it in:
bash install_launchd.sh --env-file /path/to/EQUIRE/.env.local all

# Optional failure alert: keep the URL in an owner-only file outside git, then
# render only its path into the plist. The file must be absolute, owned by the
# current user, readable, and mode 400 or 600.
umask 077
printf '%s\n' 'https://example.invalid/replace-at-provisioning' > /absolute/private/path/cre-alert-webhook.url
chmod 600 /absolute/private/path/cre-alert-webhook.url
bash install_launchd.sh \
  --alert-webhook-file /absolute/private/path/cre-alert-webhook.url all

# ONLY after operator-runbook gate 5 records Cayman's approval of the exact
# coordinator, owner, job labels, credentials, observation, and rollback:
bash install_launchd.sh --load monitor enrich

# weekly is additive by default, but its scheduler load is covered by the same
# explicit gate. CRE_WEEKLY_MARK_MISSING=1 remains a separate escalation.
bash install_launchd.sh --load weekly

# Preview a rendered plist without installing:
bash install_launchd.sh --print enrich
```

Uninstall (unload + remove the installed copies; templates are untouched):

```bash
bash install_launchd.sh --uninstall all
```

Verify run health (preferred): one read-only command summarizes every tier
(loaded? running? last exit; staleness vs cadence; last-run verdict; stack/env
state) and exits nonzero if anything is unhealthy:

```bash
bash ../cre_status.sh                # offline heartbeat
bash ../cre_status.sh --full-health  # also runs the full firecrawl healthcheck
bash ../cre_status.sh --expected-sha <merged-sha>  # deployment identity gate
```

The optional failure webhook is delivered synchronously with a ten-second
timeout so launchd cannot terminate it with the job process group. Delivery is
best-effort and never replaces the tier's original exit code.

Low-level check of a single loaded job:

```bash
launchctl list | grep ai.agentic.cre
# A non-zero PID in column 1 means the job is running now.
# Column 2 is the LAST run's exit code (0 = success; nonzero = a failed run). It is
# ephemeral (reset on reboot/reload) and says nothing about WHEN the job last
# ran, which is why cre_status.sh adds the staleness and last-run-verdict checks.
```

---

## Logs

All tiers write stdout and stderr into `out/daily/` beside the existing daily
run logs:

| tier    | stdout                          | stderr                          |
|---------|---------------------------------|---------------------------------|
| monitor | `out/daily/cre-monitor.out.log` | `out/daily/cre-monitor.err.log` |
| enrich  | `out/daily/cre-enrich.out.log`  | `out/daily/cre-enrich.err.log`  |
| weekly  | `out/daily/cre-weekly.out.log`  | `out/daily/cre-weekly.err.log`  |
| daily   | `out/daily/cre-daily.out.log`   | `out/daily/cre-daily.err.log`   |

(daily logs exist only if the retired rollback case is run.)

Each run also emits a timestamped START/END line via `cre_run_tier.sh` so you
can grep for the exact wall-clock boundaries of any tier run, and writes a
machine-readable verdict to `out/daily/last_run_<tier>.json`
(`{tier,start,end,rc,ok}`) that `cre_status.sh` reads.

The monitor tier additionally redirects its heavy child output (`collect.ts
--monitor` + `cre_monitor.py`) to a **per-run** log
`out/monitor/monitor_<stamp>.log` beside its `monitor_<stamp>.json` artifact,
rather than to the append-only launchd redirect file. This matters because each
enumeration artifact is large; left unbounded it would fill the disk over time.

**Disk is self-bounding.** On every real run (pass or fail), `cre_run_tier.sh`
`finish()` prunes runtime artifacts: it keeps the newest 24 `monitor_*.json` and
24 `monitor_*.log` under `out/monitor/` (~3 days) and caps each append-only
`cre-*.{out,err}.log` at 10 MB (trimming to the last half from the next fire on).
`cre_daily_update.sh` (driven by the weekly tier) prunes its own `out/daily/`
artifacts separately (14 `run_*.json`, 29 `run_*.log`, 14 `gate_*.json`). No
cron or manual cleanup is
needed; `cre_status.sh` still warns if the footprint grows past ~4GB
(`out/daily`) / ~8GB (`out/monitor`) as a backstop.
