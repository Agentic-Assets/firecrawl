# CRE Collector: Freshness, Accuracy, and Historic-Retention Review

**Date:** 2026-06-15
**Scope:** ingest reconciliation lifecycle (`cre_ingest.py`), schema and history retention
(`sql/002,005,007`), the Savills source cap (`sources/savills.ts`), and the freshness model
(`cre_monitor.py`, `cre_gate.py`, the launchd tiers, the design doc).
**Question driving it:** what gives us the most accurate, up-to-date data while ALWAYS keeping
old inactive listings for historic data, and what is wrong or improvable today.

**Method:** four parallel reader agents mapped each subsystem, then every concern they raised
was adversarially verified against the actual code (default stance: refute unless the code
proves it). 21 concerns examined: **14 confirmed, 6 partial, 1 refuted.** File:line citations
below are from that verified pass.

---

## 0. Executive summary

The architecture already does what you want at the row level: **removal is always a soft-delete,
never a hard `DELETE`.** A retired listing keeps its row (`deleted_at = now()`,
`status = 'inactive'`) and simply drops off the agent-facing board because the views filter it
out. History-of-existence is preserved by design.

The real problems are three:

1. **It is not running.** Both scheduled tiers exit 126 (macOS Full Disk Access / TCC block).
   Live data is only as fresh as the last manual run, and nothing retires stale rows.
2. **No retirement is loaded.** Weekly `--mark-missing` is unloaded and daily is additive, so
   the active board can only grow. Sold or pulled listings persist as `active`.
3. **Value-over-time history is a gap.** The row survives, but its price and status are
   overwritten in place. The only time-series sink (`cre_listing_events`) is written solely by
   the monitor, which has never run. So a price/status history does not actually exist yet.

There is also one true data-loss hazard to fix before retirement is ever turned on: a folded
source that returns zero rows without erroring can cause a whole brokerage to be soft-deleted.

The Savills sale cap is real and verified (no public US commercial-sale feed exists), not a
scraper bug, though the sale adapter still points at the wrong (residential) surface and should
be guarded.

---

## 1. How the goal is already met (and where it falls short)

**Soft-delete preserves history.** The only de-listing path is the `--mark-missing`
reconciliation, which runs `UPDATE ... SET deleted_at = now(), status = 'inactive'`
(`cre_ingest.py:1102-1109`). There is no `DELETE FROM credeals.cre_listings` anywhere in the
ingestor. The agent-facing views filter `deleted_at IS NULL AND status IN
('active','under_contract','pending')` (`005_cre_views.sql:120-122,158-160,187-188,250-251`),
so inactive/sold/leased rows still exist in the base table but are invisible to EQUIRE.

**The intended freshness model is three tiers:**

| Tier | Cadence | Role | Touches deleted_at? |
|------|---------|------|---------------------|
| monitor | every 3h | new-listing latency, enumeration diff, append-only event ledger | No (observe-only) |
| daily | 06:30 | content source of truth, additive upsert | No (`--no-mark-missing`) |
| weekly | Sun 03:00 | retire gone listings, the only soft-delete path | Yes (`--mark-missing`, gated) |

That blend is the correct answer to "fresh + keep history." The shortfalls are operational
(tiers 1 and 3 are not running/loaded) and one history gap (row values are not versioned). The
rest of this document is the fix list.

---

## 2. Findings, with recommendations and TODOs

Severity is the reviewer rating. Status is the adversarial verdict. Each item has a concrete
TODO and an effort estimate.

### Tier 1 - operational (these undercut the whole pipeline)

#### H1. No scheduled run has ever succeeded (TCC exit 126) `[confirmed - high]`
Both `ai.agentic.cre-daily` and `ai.agentic.cre-monitor` exit 126 on every fire. Verified live:
`launchctl list` shows `126` for both, no `out/daily/last_run_*.json` markers exist, and the err
logs carry `getcwd: ... Operation not permitted` and `/bin/bash: .../cre_run_tier.sh: Operation
not permitted`. Cause: the repo lives under `~/Documents` (TCC-protected) and the launchd
user-agent's `/bin/bash` lacks Full Disk Access. The empty event ledger is therefore a
non-signal, not evidence of "no changes."

- **Recommendation:** resolve TCC. This is the single highest-leverage fix and gates everything
  else. Full step-by-step runbook in section 4.
- **TODO:** on the Mac mini, clone outside `~/Documents` (preferred) OR grant `/bin/bash` Full
  Disk Access; then `launchctl kickstart -k gui/$(id -u)/ai.agentic.cre-daily` and confirm
  `cre_status.sh` shows rc 0.
- **Effort:** operational, minutes. Tracked as task #41.

#### H2. Retirement path is not loaded, so the board only ratchets up `[confirmed - high]`
Weekly (`--mark-missing`) is intentionally unloaded and daily runs `--no-mark-missing`. The
additive upsert actively forces every seen row back to `active` and clears `deleted_at`
(`cre_ingest.py:933,960`). Net: a sold listing that stays in the feed, or one the source quietly
drops, is never retired. The active count can only grow.

- **Recommendation:** after H1 and M1 are resolved, load weekly under the gate.
- **TODO:** `bash launchd/install_launchd.sh --load weekly` only after `cre_gate.py` is proven on
  a Tier-1 source and the count-aware coverage fix (M1) has shipped. Add the signal-staleness
  alarm (H3) first.
- **Effort:** operational + small. Tracked as tasks #37 (load) and #39 (first reconcile).

#### H3. Disappearance-only sources detect sales late or never; no staleness alarm `[confirmed - high]`
CBRE (~19k rows), NAI, Avison, Marcus have no status field. Vanishing from a full sweep is the
only sold-signal, and that fires at most weekly (~14-day detection), indefinitely if the source
is quarantined by the gate. `cre_gate.py` is a one-sided lower-bound coverage gate only
(`cre_gate.py:147-184`); there is no signal-staleness alert. This is the design's own open item
(section 12.8).

- **Recommendation:** add a per-source signal-staleness monitor that alarms when a
  disappearance-only source exceeds its expected cadence, then enable weekly with confidence.
- **TODO:** surface per-source `last_enumerated_at` vs cadence in `cre_status.sh` (or a new
  check) and alert on CBRE/NAI/Avison/Marcus staleness before re-enabling mark-missing.
- **Effort:** medium.

### Tier 2 - the history gap (directly tied to your stated goal)

#### H4. Row-level price/status history is not preserved `[confirmed - high]`
The soft-delete keeps the row, but every upsert overwrites `sale_price_usd`, `sale_price_per_sf`,
`lease_rate_min/max`, and `status` in place (`cre_ingest.py:949-953,933`). The only history sink
is the append-only `cre_listing_events` ledger, written exclusively by the monitor
(`cre_monitor.py:588`), and the monitor has never run (H1). So today there is effectively no
captured price/status time series. Even when the monitor runs, `price_change` events store
`old_value = NULL` because `cre_source_index` keeps only a one-way hash fingerprint, not the
prior dollar amount (`cre_monitor.py:417-433`).

- **Recommendation:** make history a property of ingest, not of the (currently blocked) monitor.
  Add an append-only `cre_listing_price_history` / `cre_listing_snapshots` table that the
  ingestor writes whenever a watched field (price, status, cap_rate) changes, capturing
  `(listing_id, observed_at, sale_price_usd, lease_rate_min/max, status, cap_rate,
  source_lastmod)`. Separately, persist the prior price in `cre_source_index` so monitor
  `price_change` events carry a real `old_value`.
- **TODO (a):** add the history table to `sql/` and write to it in `build_sql()` on a real diff.
- **TODO (b):** add `prior_sale_price` / `prior_lease_rate` columns to `cre_source_index`
  (`sql/007`) and populate `old_value` in `cre_monitor.py:417-433`.
- **Effort:** medium (a), small (b).

#### M2. Child rows and raw snapshots are discarded on re-scrape `[confirmed - medium]`
Contacts, documents, and images are hard-deleted and wholesale-replaced on every clean re-scrape
(`cre_ingest.py:1061-1063`); `raw_data` is overwritten, not versioned (`:957`). So who brokered a
now-sold deal, or a withdrawn brochure, is gone after the next scrape.

- **Recommendation:** add `deleted_at` to the child tables (soft-delete instead of wholesale
  DELETE), or an append-only child-history table; optionally archive prior `raw_data`.
- **TODO:** replace the child `DELETE`/re-`INSERT` with a soft-delete + insert-new pattern, or
  decide explicitly that child history is out of scope and document it.
- **Effort:** medium.

#### M3. mark-missing soft-delete writes no `disappeared` event `[confirmed - medium]`
The `--mark-missing` UPDATE sets `deleted_at`/`status='inactive'` but does not insert a
`cre_listing_events` row in the same transaction (`cre_ingest.py:1102-1109`); the `disappeared`
event is produced only by the separate monitor path. So a listing retired by the ingestor can
have no ledger entry marking when/why it went inactive.

- **Recommendation:** emit a `disappeared` event (old_value = prior status, new_value =
  'inactive', detected_at = now()) in the same transaction as the soft-delete.
- **TODO:** add the `INSERT INTO credeals.cre_listing_events` to the mark-missing block.
- **Effort:** small. Pairs with H4(a).

### Tier 3 - correctness hazards to fix BEFORE turning on retirement

#### M1. mark-missing can soft-delete a whole brokerage on a silent empty folded source `[confirmed - medium] (BUG)`
The ingestor's own folded-coverage check is key-presence-only, not count-aware
(`cre_ingest.py:1285`: `len(known_keys) == 1 or known_keys <= seen_keys`). A folded source (for
example `colliers-main`) that returns zero rows without erroring still counts as "covered." If
the parent clears the 100-row floor, every row from the empty folded source is soft-deleted: the
~15,829 `colliers-main` rows could be wiped in one run. The count-aware guard exists only in
`cre_gate.py`, which is observe-only and not consulted by the ingestor's eligibility. The daily
script's gate step catches this, but a manual `python3 cre_ingest.py --in run.json
--mark-missing` does not.

- **Recommendation:** make ingest eligibility count-aware. Either require each folded key to
  stage a minimum count or share of its baseline, or have `cre_ingest.py` consume the gate JSON's
  per-source `mark_missing_safe` verdict so ingest and gate share one decision.
- **TODO:** patch `has_complete_folded_coverage` (`cre_ingest.py:1283-1296`) to require a nonzero
  staged count per folded key, and/or wire `cre_gate.py`'s rollup into the ingestor. Add a unit
  test for the empty-but-error-free folded-source case.
- **Effort:** medium. **Must precede loading weekly (H2/#39).**

#### L1. Price fields force-overwritten with NULL on a parse miss `[confirmed - low] (BUG)`
The four price columns overwrite with `EXCLUDED` unconditionally (`cre_ingest.py:949-953`),
unlike neighbors that `COALESCE`-keep. A transient parse miss ("Call for offer", regex miss)
nulls a previously-good numeric price.

- **Recommendation:** `COALESCE(EXCLUDED.x, t.x)` on the price columns so a transient miss does
  not blank known-good data; model a real "price withheld" transition with an explicit flag
  instead of overwrite-with-NULL.
- **TODO:** change `cre_ingest.py:949-953` to COALESCE-keep; add a regression test.
- **Effort:** small.

#### M5. Revival resets soft-deleted rows to active, losing terminal status `[partial - medium]`
A row that drops out and reappears is unconditionally reset to `active` and un-deleted
(`cre_ingest.py:933,960`), so a sold listing that flickers back into a feed loses its terminal
label. Verified real, but documented as deliberate recovery semantics and only harmful once
Phase-2 status activation is ON (default-off today). Latent, not live.

- **Recommendation:** reset to `active` on revival only when the prior status was `inactive` (a
  mark-missing soft-delete), not a real terminal (`sold`/`leased`/`off_market`).
- **TODO:** tighten the CASE at `cre_ingest.py:933` before enabling status activation.
- **Effort:** small.

#### L2. Retention is convention-only, not DB-enforced `[confirmed - low]`
No trigger, partition, or revoked privilege prevents a future migration from hard-deleting
soft-deleted rows; child FKs are `ON DELETE CASCADE`. The "always keep history" guarantee is
policy, not enforcement. (No current code path hard-deletes listings; the repo's own advisor
report confirms "no production delete path today.")

- **Recommendation:** for a hard guarantee, add a `BEFORE DELETE` trigger that raises on
  `deleted_at IS NOT NULL` rows, or move terminal rows into a `cre_listings_history` partition;
  add a `deleted_at` index to keep history scans cheap.
- **TODO:** decide whether a DB-level guard is warranted; if so, add the trigger + index in
  `sql/`.
- **Effort:** medium.

#### L4. Status-flip circuit breaker is default-off `[partial - low]`
`CRE_STATUS_FLIP_MAX_FRACTION` is unset by default, the trip metric counts only rows leaving
`active` (not `under_contract->sold`), and sources below 200 active rows are exempt
(`cre_ingest.py:803-805,992-994,1011`). Verified, but non-load-bearing today because status
activation is default-off, so there are zero flips to guard.

- **Recommendation:** when status activation is enabled for scheduled runs, set a conservative
  `CRE_STATUS_FLIP_MAX_FRACTION` in the launchd env and broaden the trip metric to count any
  non-active reclassification; reconsider the 200-row exemption.
- **TODO:** add the env var to the rendered plists and widen the metric at
  `cre_ingest.py:992-994` as part of the status-activation go-live.
- **Effort:** small.

### Savills (see section 3 for the full explanation)

#### L5. Sale path targets the residential surface with no IsCommercial filter `[partial - medium] (BUG/risk)`
The sale branch hits `/com/en/list/property-for-sale/...` (generic, residential) and applies no
`IsCommercial` filter, unlike the lease branch (`savills.ts:280,296-350` vs `:221`). That is why
101 residential homes were ingested and had to be soft-deleted on 2026-06-14. If the sale
adapter ever runs additively again it can silently re-ingest residential homes.

- **Recommendation:** add an `IsCommercial`/commercial-surface guard to the sale path so
  residential contamination cannot recur, even though the cap stands.
- **TODO:** mirror the lease filter in the sale branch; if a commercial-sale `__NEXT_DATA__`
  route ever appears, collapse the sale path onto `savillsNextDataProperties()`.
- **Effort:** small.

#### L3. Savills commercial-lease path is single-page (latent undercount) `[confirmed - low]`
`srcSavillsCommercialLease` fetches one page and slices it with no pagination loop, while the
sale path paginates (`savills.ts:218-222` vs `:285-287`). `savillsTotalItems()` is read but not
used to drive pagination. Moot today at 2 rows, but a latent gap in the one path that works.

- **Recommendation:** paginate the lease path using `listPage.totalItems`, and set the
  `truncated` flag so the monitor can gate disappearance events correctly.
- **TODO:** add a `/page/N` loop to `srcSavillsCommercialLease` mirroring the sale loop.
- **Effort:** small.

### Known / intentional (documented, lower priority)

#### M4. Four detail-id sources excluded from monitor `[confirmed - medium]`
`jll`, `jll-investor`, `cbre-dealflow`, and colliers `SalesTracker` have detail-derived
external_ids unrecoverable from cheap enumeration, so they short-circuit monitor mode and return
zero rows (`jll.ts:384` and siblings). No sub-daily detection for them; full-sweep cadence only.
`colliers-main` (sitemap ids) remains monitor-enabled. Documented design choice; a cheap path
would require URL-keyed reconciliation in `cre_monitor.py` (not built).

- **Recommendation:** accept for now; revisit if sub-daily latency on these four becomes a
  priority.
- **Effort:** large (deferred).

#### M6 / L6. Disappearance-only status lifecycle and source_lastmod / price_change noise `[partial]`
Two partial findings: (M6) for empty-path sources the only state transition is the mark-missing
soft-delete, though `norm_status`'s text fallback can still emit a terminal from a title/slug
(the concern's "always NULL" claim was inaccurate); (L6) `source_lastmod` trust varies per
source and `price_change` carries `old_value = NULL`, but the raw-text mitigation already exists
in the fingerprint, so the live impact is smaller than stated. Both are tracked design caveats
(sections 12.6, 12.9), not live bugs.

- **Recommendation:** fold into the H4(b) prior-price work and the per-source lastmod
  verification already deferred in the design doc.
- **Effort:** small-medium, deferred.

### Refuted

#### R1. "Savills commercial-sale URL is un-probed" - FALSE `[refuted]`
The concern claimed the cap is unverified because the commercial-sale URL was never tried. It was
tried. `SAVILLS_US_SALE_PUBLIC_PATH_RECHECK_2026-06-12.md` records a 22-URL test matrix; the
exact URL returned HTTP 200 with `totalItems: 0`, and every commercial-sale variant returned 0
rows or non-US (Canada/UK/Ireland) objects. The cap is verified. **One real doc nit remains:**
`START_HERE.md:276-277` still calls that URL "un-probed," contradicting the recheck doc and the
adapter comment (`savills.ts:270`). Fix the wording.

- **TODO:** correct `START_HERE.md:276-277` to say the commercial-sale route was probed and ruled
  out (US commercial sale not publicly exposed), not "un-probed."
- **Effort:** trivial (one line).

---

## 3. Why Savills sale is structurally capped

This is a true data-availability limit, not a fixable scraper bug. The adapter has two separate
paths (`sources/savills.ts`):

- **Lease works.** It hits the commercial-namespaced URL
  `/com/en/list/commercial/property-to-let/united-states-of-america`, parses the embedded
  `__NEXT_DATA__` JSON, and filters `IsCommercial === true` (`savills.ts:219-221`). Clean
  structured commercial data.
- **Sale points at the wrong surface.** It hits the generic
  `/com/en/list/property-for-sale/united-states-of-america` (no `/commercial/` segment) and parses
  HTML cards with no `IsCommercial` filter (`savills.ts:280,296-350`). That URL is Savills
  Residential luxury homes, which is why 101 residential houses were ingested and soft-deleted on
  2026-06-14, leaving 2 defensible Chicago retail leases.

The structural part: the commercial-sale analogue
`/com/en/list/commercial/property-for-sale/united-states-of-america` was probed (a 22-URL matrix,
HTTP 200, `totalItems: 0`, only a Toronto Canada object). Savills does not expose US commercial
for-sale inventory on its public search the way it does for to-let. Hence "no public US
commercial-sale feed." The remaining coverage (2 lease rows) is the permanent public baseline
unless a non-public route is licensed.

Operational consequence: keep daily ingest `--no-mark-missing` while Savills sale stays capped,
so the tiny/empty sale side does not trigger a disappearance soft-delete.

---

## 4. Item H1 in detail: granting operational (Full Disk Access) permission

The scheduled jobs run as a macOS **launchd user agent**. macOS TCC (Transparency, Consent, and
Control) blocks any process from reading files under protected folders (`~/Documents`,
`~/Desktop`, `~/Downloads`, iCloud Drive) unless the binary that does the reading has been
granted **Full Disk Access**. The plist runs `/bin/bash <repo>/.../cre_run_tier.sh`, so when the
repo is under `~/Documents`, `/bin/bash` cannot even read the script and the job exits 126.

There are two clean ways to fix this. **Option A is strongly recommended for the Mac mini**
because it needs no system grant and no broad permission.

### Option A (recommended): clone outside `~/Documents`

A clone outside the TCC-protected folders needs no Full Disk Access grant at all.

```bash
# On the Mac mini, clone to a non-protected path:
git clone <repo-url> ~/code/firecrawl
cd ~/code/firecrawl

# Bring up the stack and bootstrap (see SETUP.md):
docker compose up -d
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
export CRE_ENV_FILE=/path/to/EQUIRE/.env.local
cd scripts/firecrawl-ops/cre_collector
bash cre_setup.sh                       # expect "0 FAIL"; it confirms you are clear of TCC

# Render + install the schedules (no auto-load), then load the safe tiers:
bash launchd/install_launchd.sh all
bash launchd/install_launchd.sh --load monitor daily

# Prove a run and check health:
launchctl kickstart -k gui/$(id -u)/ai.agentic.cre-daily
bash cre_status.sh                      # last-run rc should be 0, not 126
```

`cre_setup.sh` section 6 explicitly checks the clone path and reports "Clone is outside
~/Documents (no TCC blocker for launchd)" when you are clear. `~/code` and `~/srv` are good
choices; avoid `~/Documents`, `~/Desktop`, `~/Downloads`, and any iCloud-synced folder.

### Option B: keep the repo where it is and grant `/bin/bash` Full Disk Access

Use this only if you want to keep the clone under `~/Documents`. It is broader than Option A
because it grants every bash script on the machine full disk access.

1. Open **System Settings > Privacy & Security > Full Disk Access**.
2. Click the **+** button (you may be prompted for your password / Touch ID).
3. In the file picker press **Cmd+Shift+G** and type the path: `/bin/bash` then press Return and
   **Open**. (`/bin/bash` is hidden in the normal picker, which is why you type the path.)
4. Confirm the toggle next to **bash** is **on**.
5. Reload the jobs so they pick up the new permission:

   ```bash
   launchctl kickstart -k gui/$(id -u)/ai.agentic.cre-daily
   launchctl kickstart -k gui/$(id -u)/ai.agentic.cre-monitor
   ```

6. Verify:

   ```bash
   launchctl list | grep ai.agentic.cre   # column 2 should be 0, not 126
   bash cre_status.sh                      # last-run verdict OK, no TCC warning
   ```

Notes and gotchas:
- `chmod +x` is NOT the fix. The block is TCC permission, not the execute bit.
- If you run the pipeline manually from a terminal, the **terminal app** (Terminal.app, iTerm,
  or your IDE) also needs Full Disk Access for manual runs under `~/Documents`. Scheduled runs
  only need `/bin/bash` to have it.
- If you use a Homebrew bash, the plist would point at that binary instead; grant Full Disk
  Access to whatever `ProgramArguments[0]` is in the installed plist (check with
  `plutil -p ~/Library/LaunchAgents/ai.agentic.cre-daily.plist`). The installer uses `/bin/bash`.
- After macOS upgrades, TCC grants occasionally need to be re-confirmed.

### Which to choose

For the Mac mini production box, use **Option A** (clone to `~/code/firecrawl`). It is the
cleanest, needs no system-wide permission, and `cre_setup.sh` verifies it for you. Reserve
Option B for the case where relocation is not possible.

---

## 5. Prioritized remediation roadmap

Ordered to maximize accurate up-to-date data while preserving full history, safest-first:

1. **H1 - resolve TCC** (section 4). Operational, minutes. Nothing else matters until the tiers
   run. *(task #41)*
2. **M1 - count-aware folded coverage** (the data-loss guard). Must ship before any
   `--mark-missing`. *(new)*
3. **H4(a/b) + M3 - ingest-written history** (price/status snapshots, prior-price in
   source_index, disappeared event on soft-delete). Closes the value-over-time gap. *(new)*
4. **H3 - signal-staleness alarm** for disappearance-only sources. *(new)*
5. **H2 - load weekly `--mark-missing` under the gate**, once 2 and 4 are in. *(tasks #37, #39)*
6. **Smaller hardening:** L1 (price COALESCE), L5 (Savills IsCommercial guard), M5 (revival
   terminal-stickiness), L3 (Savills lease pagination), R1 (Savills doc fix). *(new)*
7. **Deferred / optional:** M2 (child-row history), L2 (DB-enforced retention), L4 (flip-breaker
   defaults), M4/M6/L6 (sub-daily detection, lastmod verification).

Items 2, 3, and 6 are net-new code changes scoped on this branch. Items 1, 5 are operational and
gated. None of the gated go-lives (#36 consumer deploy, #37 load, #39 first reconcile, #41 TCC)
should be actioned without explicit go-ahead.

---

## 6. Provenance

Produced by a multi-agent review on 2026-06-15 (4 readers + adversarial verification of each
concern; 25 agents total). Verdict tally: 14 confirmed, 6 partial, 1 refuted. Every file:line
citation was checked against the working tree at the time of review. Related: `START_HERE.md`
(live counts and per-source status), `CLAUDE.md` (collector reference),
`cre-intelligence-system-design.md` sections 9 and 12-14 (the design's own open-item list, which
several findings independently confirm).

---

## 7. Resolution (2026-06-15)

All code, SQL, and tests for this review were implemented by 5 parallel agents
(A-E) on branch `feat/cre-brokerage-collectors-2026-06-12`. Below is a
per-item account of what SHIPPED versus what remains DEFERRED or GATED.

### Items SHIPPED (code + SQL + tests)

**M1 (count-aware folded coverage, BUG)** - SHIPPED in `cre_ingest.py`.
`discovered_by_source_key` dict accumulates `listingsCollected` per `sourceKey`
from `source_entries`. `has_complete_folded_coverage` now requires each folded
key to have a nonzero count (singletons still short-circuit on `len(known_keys)
== 1`). Tests: `tests/test_folded_coverage_count_aware.py` (7 tests: M1 bug
scenario, control, singleton regression, L4a flip-metric assertion).

**L1 (price COALESCE-keep, BUG)** - SHIPPED in `cre_ingest.py`. All four price
columns (`sale_price_usd`, `sale_price_per_sf`, `lease_rate_min`,
`lease_rate_max`) now use `COALESCE(EXCLUDED.x, t.x)` in the upsert
`DO UPDATE SET` block, matching `cap_rate`. Tests:
`tests/test_price_coalesce.py` (9 tests).

**M5 (revival terminal-stickiness)** - SHIPPED in `cre_ingest.py`. The revival
CASE now reads `AND t.status = 'inactive'` so only mark-missing-soft-deleted
rows (not real terminals) are reset to `active`. Tests:
`tests/test_revival_terminal_stickiness.py` (4 tests). NOTE: the existing
assertion in `test_ingest_status_activation.py::
test_update_keeps_status_sticky_resetting_only_resurrected` is now stale (it
asserts the old unconditional CASE form); it is in a file owned by neither
Agent A nor any other review owner and must be updated by the integration owner
before merging.

**M3 (disappeared event on mark-missing)** - SHIPPED in `cre_ingest.py`. The
mark-missing block is now a CTE chain: `CREATE TEMP TABLE _retired ON COMMIT
DROP AS SELECT l.id, l.brokerage_id, l.status AS prior_status ...`; the
soft-delete UPDATE runs `FROM _retired`; an `INSERT INTO
credeals.cre_listing_events` with `event_type='disappeared'`, `field='status'`,
`old_value=r.prior_status`, `new_value='inactive'`,
`source_value='mark_missing'` follows in the same transaction. Tests:
`tests/test_disappeared_event_on_mark_missing.py` (8 tests).

**H4a-write (price-history INSERT in ingest)** - SHIPPED in `cre_ingest.py`.
`CREATE TEMP TABLE _prior_vals` captures watched fields BEFORE the `_up` upsert.
After the upsert and the status-activation UPDATE, a price-history INSERT
selects rows where any of the 6 watched fields IS DISTINCT FROM the prior value.
Existence-guarded via `to_regclass('credeals.cre_listing_price_history')` (the
guard is emitted in apply mode; dry-run emits the plain INSERT so tests can
assert the shape). New `history_guard: bool = True` parameter on `build_sql()`;
`main()` passes `history_guard=not args.dry_run`. Tests:
`tests/test_price_history_snapshot.py` (9 tests).

**M2-archive-write (contacts + documents snapshot at retirement)** - SHIPPED in
`cre_ingest.py`. Inside the `if mark_missing_slugs:` block, after the `_retired`
CTE, existence-guarded INSERTs snapshot the retired listings' final contacts and
documents into `cre_listing_contacts_archive` and
`cre_listing_documents_archive`. Images explicitly excluded. Tests:
`tests/test_child_history_archive_on_retirement.py` (7 tests).

**L4a (widen flip-breaker trip metric)** - SHIPPED in `cre_ingest.py`.
`leaving_active` FILTER now counts any reclassification to a non-active status
(not only departures from `active`), with a terminal-progression exemption.
Assertion in `tests/test_disappeared_event_on_mark_missing.py`.

**H4a-table (cre_listing_price_history)** - SHIPPED in
`sql/009_cre_history_retention.sql`. Table with 10-column contract
(`listing_id, observed_at, sale_price_usd, sale_price_per_sf, lease_rate_min,
lease_rate_max, status, cap_rate, source_lastmod, transaction_type`), FK ON
DELETE CASCADE to `cre_listings`, composite index on `(listing_id,
observed_at DESC)`, RLS enabled. NOT YET APPLIED to prod (gated; existence
guard in ingestor makes pre-apply runs a no-op).

**H4b-columns (prior_* on cre_source_index)** - SHIPPED in
`sql/009_cre_history_retention.sql`. Three `ADD COLUMN IF NOT EXISTS` statements
on `credeals.cre_source_index`: `prior_sale_price numeric`, `prior_lease_rate
numeric`, `prior_status text`. NOT YET APPLIED to prod (gated; monitor's read
path assumes columns exist post-migration, consistent with monitor being gated).

**M2-archive-tables (contacts + documents archive)** - SHIPPED in
`sql/009_cre_history_retention.sql`. `cre_listing_contacts_archive` (11 columns
plus `id`/`archived_at`) and `cre_listing_documents_archive` (4 columns plus
`id`/`archived_at`), no FK to `cre_listings` (survives a future hard delete),
RLS enabled. NOT YET APPLIED to prod (gated).

**L2-trigger+index (retention guard)** - SHIPPED in
`sql/009_cre_history_retention.sql`. `credeals.cre_block_history_delete()`
BEFORE DELETE trigger function raises on any `deleted_at IS NOT NULL` row unless
`cre.allow_history_delete = 'on'`. Trigger `trg_cre_listings_block_history_delete`
on `credeals.cre_listings`. Partial index `cre_listings_deleted_at_idx` on
`(deleted_at) WHERE deleted_at IS NOT NULL`. NOT YET APPLIED to prod (gated).

**000_run_all.sql** - UPDATED: `009_cre_history_retention.sql` registered after
008 and before 006 in the dependency order comment, Option B command list, and
the executable body.

**H4b-populate (prior price in monitor)** - SHIPPED in `cre_monitor.py`.
`load_prior_state()` now reads `prior_sale_price`, `prior_lease_rate`,
`prior_status` from `cre_source_index` (COALESCE text casts, local
`_num_or_none()` helper). `derive_events()` price_change branch populates
`old_value` from `prior.get('prior_sale_price')` with lease-rate fallback.
`_ENUM_COLS` and the COPY loop now carry `cur_sale_price` and `cur_lease_rate`;
the `cre_source_index` INSERT/ON CONFLICT maps this run's values to the `prior_*`
columns for the next run. Tests: `tests/test_monitor_old_value.py` (10 tests).

**L5 (Savills IsCommercial sale-path guard)** - SHIPPED in
`sources/savills.ts`. Exported `savillsSaleCardIsCommercial()` helper with
word-boundary regex for residential keywords and commercial keyword/href checks.
Applied in the sale loop; non-commercial cards are filtered and counted in
`nonCommercialFiltered`. The terminal error guard spares the non-commercial case.

**L3 (Savills lease pagination)** - SHIPPED in `sources/savills.ts`.
`srcSavillsCommercialLease()` now paginates via `/page/N`, mirrors the sale
loop, and uses an exported `mapSavillsLeaseRow()` helper (pure, unit-testable).
`truncated: true` is set when collected count is less than
`Math.min(max, total)`. Tests: `tests/ts/sources/savills-commercial.test.ts`
(12 tests).

**R1 (Savills doc fix)** - SHIPPED in `START_HERE.md`. The "One un-probed path"
wording at line 276-277 is corrected to state the route WAS probed (22-URL
matrix, HTTP 200, `totalItems:0`) and the cap is confirmed.

**H3 (disappearance-only signal staleness in cre_status.sh)** - SHIPPED in
`cre_status.sh`. New read-only section before the summary checks the four
no-status-field sources (`cbre`, `nai-global`, `avison-young`,
`marcus-millichap`) against an 8-day threshold using local
`out/monitor/monitor_*.json` artifact mtimes. Fresh clones with no artifacts
emit a note, not a warning. Stale sources increment PROBLEMS via `warn()`.

**L4b (CRE_STATUS_FLIP_MAX_FRACTION in plist templates)** - SHIPPED in
`launchd/ai.agentic.cre-daily.plist.template` and
`launchd/ai.agentic.cre-weekly.plist.template`. `CRE_STATUS_FLIP_MAX_FRACTION`
key set to `0.30` in both templates, with an XML comment noting it is inert
until status activation is enabled.

### Items DEFERRED

**H1 (TCC / Full Disk Access)** - DEFERRED (operational). Fix is a one-time
Full Disk Access grant to `/bin/bash` or a repo clone outside `~/Documents`.
No code change needed; see `START_HERE.md` Known Limits and section 4 of this doc.

**H2 (load weekly mark-missing)** - GATED. Do not load the weekly reconcile
tier until M1 (now shipped), H3 (now shipped), H1 (TCC), and the gate are
proven on a live run.

**M4 (four detail-id monitor exclusions)** - DEFERRED by design. No change;
the exclusions are intentional and documented.

**M6/L6 (disappearance-only lifecycle, source_lastmod noise)** - DEFERRED. Fold
into future H4(b) prior-price work and per-source lastmod verification.

**L4 min_base exemption** - DEFERRED. The 200-row exemption in the flip breaker
is left unchanged (review's "reconsider" is deferred per spec).

**cre_enrichment_queue drain worker** - DEFERRED. Not built; Tier-B enrichment
queue drain remains out of scope.

### Items GATED for explicit go-ahead

The following remain unchanged and require Cayman's go-ahead before actioning:

- Apply `009_cre_history_retention.sql` to prod (all new tables / columns /
  trigger become live). Must precede any history writes, but the ingestor runs
  safely before apply via the existence guard.
- Deploy the EQUIRE consumer board-gate branch (must precede T3.1 status
  activation).
- Trigger the first live T3.1 status activation (`--activate-status` /
  `CRE_ACTIVATE_STATUS=1`).
- Apply the widened agent-facing `005` views (`status IN
  ('active','under_contract','pending')`).
- Load the weekly reconcile tier (`ai.agentic.cre-weekly`).
