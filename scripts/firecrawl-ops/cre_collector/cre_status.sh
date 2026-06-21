#!/usr/bin/env bash
# =============================================================================
# cre_status.sh: read-only health / heartbeat for the CRE collector.
#
# One command that answers "is the scheduled pipeline alive and healthy?" so an
# operator (or a coding agent on the Mac mini) does not have to cross-reference
# `launchctl list`, log tails, and per-run markers by hand. It reports, per
# tier (monitor / enrich / daily / weekly):
#   - launchd state: loaded? running now? last exit code (flags TCC-126).
#   - freshness: time since the newest run artifact vs the expected cadence,
#     so a schedule that has silently stopped firing is surfaced as STALE.
#   - last-run verdict: from out/daily/last_run_<tier>.json (written by
#     cre_run_tier.sh) plus the success sentinel in the newest run log.
# Plus: last-ingest row counts (offline, from the newest daily log), Firecrawl
# stack reachability, the ~/Documents TCC blocker, env-file discoverability
# (path only, never the URL), and the tail of the newest launchd stderr log.
#
# Read-only and secret-free: no DB connection, no launchctl mutation, no
# POSTGRES_URL ever printed. Exits nonzero if any problem is detected, so it
# can double as a lightweight watchdog.
#
# Usage:
#   bash cre_status.sh                 # offline status (default)
#   bash cre_status.sh --full-health   # also run the full firecrawl healthcheck
#
# Deliberately `set -uo pipefail` (no -e): every check runs and the script
# aggregates a single PASS/PROBLEM verdict at the end.
# =============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
OUT_DAILY="$DIR/out/daily"
OUT_MONITOR="$DIR/out/monitor"
OUT_ENRICH="$DIR/out/enrich"
FC_DIR="${FC_DIR:-$DIR/../../..}"
API_URL="${API_URL:-http://localhost:3002}"

FULL_HEALTH=0
for a in "$@"; do
  case "$a" in
    --full-health) FULL_HEALTH=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a (try --full-health)" >&2; exit 2 ;;
  esac
done

PROBLEMS=0
ok()      { printf '  OK    %s\n' "$1"; }
warn()    { printf '  WARN  %s\n' "$1"; PROBLEMS=$((PROBLEMS+1)); }
bad()     { printf '  FAIL  %s\n' "$1"; PROBLEMS=$((PROBLEMS+1)); }
note()    { printf '        %s\n' "$1"; }
section() { printf '\n== %s ==\n' "$1"; }

now_epoch="$(date +%s)"

# 1.5x the nominal cadence: monitor 2x/day (12h), enrich every 4h, daily 1d,
# weekly 7d. Daily is retired in the new design, but remains reported while the
# old live tier is still loaded as a rollback path.
stale_threshold() {
  case "$1" in
    monitor) echo $(( 18 * 3600 )) ;;          # 18h  (1.5x the 12h 2x/day cadence)
    enrich)  echo $(( 6 * 3600 )) ;;           # 6h   (1.5x the 4h cadence)
    daily)   echo $(( 36 * 3600 )) ;;          # 36h
    weekly)  echo $(( 10 * 86400 )) ;;         # 10d
  esac
}

newest_artifact() {
  case "$1" in
    monitor)      ls -t "$OUT_MONITOR"/monitor_*.json 2>/dev/null | head -1 ;;
    enrich)       ls -t "$OUT_ENRICH"/*.json 2>/dev/null | head -1 ;;
    daily|weekly) ls -t "$OUT_DAILY"/run_*.log 2>/dev/null | head -1 ;;
  esac
}

file_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null; }

human_age() {
  local s="$1"
  if   [ "$s" -ge 86400 ]; then echo "$(( s / 86400 ))d $(( s % 86400 / 3600 ))h"
  elif [ "$s" -ge 3600 ];  then echo "$(( s / 3600 ))h $(( s % 3600 / 60 ))m"
  else echo "$(( s / 60 ))m"
  fi
}

human_kb() {
  local kb="$1"
  if   [ "$kb" -ge 1048576 ]; then echo "$(( kb / 1048576 )).$(( kb % 1048576 * 10 / 1048576 ))G"
  elif [ "$kb" -ge 1024 ];    then echo "$(( kb / 1024 ))M"
  else echo "${kb}K"
  fi
}

marker_field() {  # marker_field <file> <key>  (numbers, bare words, and quoted strings)
  # Strip only the leading "key": and any surrounding quotes; a greedy s/.*://
  # would eat through the internal colons of an ISO-8601 timestamp value.
  grep -o "\"$2\":[^,}]*" "$1" 2>/dev/null | head -1 \
    | sed -E 's/^"[^"]*":[[:space:]]*//; s/^"//; s/"$//'
}

dir_size_kb() { du -sk "$1" 2>/dev/null | awk '{print $1}'; }

# ---------------------------------------------------------------------------
section "launchd schedules"
# ---------------------------------------------------------------------------
LCTL="$(launchctl list 2>/dev/null | grep 'ai.agentic.cre' || true)"
tier_loaded() {
  local label="ai.agentic.cre-$1"
  printf '%s\n' "$LCTL" | awk -v l="$label" '$3==l {found=1} END {exit found ? 0 : 1}'
}
for tier in monitor enrich daily weekly; do
  label="ai.agentic.cre-$tier"
  line="$(printf '%s\n' "$LCTL" | awk -v l="$label" '$3==l {print; exit}')"
  if [ -z "$line" ]; then
    if [ "$tier" = "daily" ]; then
      note "$tier: not loaded (retired/cutover target; reported when still live)"
    elif [ "$tier" = "weekly" ]; then
      note "$tier: not loaded (intentional: weekly is held until reconcile is approved)"
    else
      note "$tier: not loaded"
    fi
    continue
  fi
  pid="$(printf '%s' "$line" | awk '{print $1}')"
  status="$(printf '%s' "$line" | awk '{print $2}')"
  if [ "$pid" != "-" ]; then
    ok "$tier: loaded, RUNNING now (pid $pid)"
  elif [ "$status" = "0" ]; then
    ok "$tier: loaded, last exit 0"
  elif [ "$status" = "126" ]; then
    bad "$tier: loaded, last exit 126 (macOS TCC / Full Disk Access block; see SETUP.md)"
  else
    bad "$tier: loaded, last exit $status"
  fi
done

# ---------------------------------------------------------------------------
section "last run per tier"
# ---------------------------------------------------------------------------
for tier in monitor enrich daily weekly; do
  marker="$OUT_DAILY/last_run_$tier.json"
  verdict=""
  marker_problem=""
  if [ -f "$marker" ]; then
    if [ ! -s "$marker" ]; then
      marker_problem="empty marker: ${marker#"$DIR"/}"
    else
      rc="$(marker_field "$marker" rc)"
      ok_field="$(marker_field "$marker" ok)"
      end_field="$(marker_field "$marker" end)"
      if [ -z "$rc" ] || [ -z "$ok_field" ] || [ -z "$end_field" ]; then
        marker_problem="malformed marker: ${marker#"$DIR"/}"
      else
        verdict="rc=${rc} ok=${ok_field} end=${end_field}"
      fi
    fi
  fi
  art="$(newest_artifact "$tier")"
  if [ -z "$art" ]; then
    note "$tier: no run artifacts yet (never run on this clone)"
    continue
  fi
  mt="$(file_mtime "$art")"
  age=$(( now_epoch - ${mt:-now_epoch} ))
  thr="$(stale_threshold "$tier")"
  agestr="$(human_age "$age")"
  if [ -n "$marker_problem" ]; then
    if [ "$tier" = "daily" ] && ! tier_loaded "$tier"; then
      note "$tier: retired/unloaded; ignoring stale ${marker_problem} (artifact ${agestr} ago)"
    else
      bad "$tier: ${marker_problem} (artifact ${agestr} ago)"
    fi
  elif [ "$age" -gt "$thr" ]; then
    if ! tier_loaded "$tier"; then
      note "$tier: last artifact ${agestr} ago (tier is not loaded) [${verdict:-no marker}]"
    elif [ "$tier" = "weekly" ]; then
      note "$tier: last artifact ${agestr} ago (weekly may be intentionally unloaded) [${verdict:-no marker}]"
    else
      warn "$tier: STALE, last artifact ${agestr} ago (expected < $(human_age "$thr")) [${verdict:-no marker}]"
    fi
  elif printf '%s' "$verdict" | grep -q 'ok=false'; then
    bad "$tier: last run FAILED [${verdict}] (artifact ${agestr} ago)"
  else
    ok "$tier: last artifact ${agestr} ago [${verdict:-no marker}]"
  fi
done

# ---------------------------------------------------------------------------
section "last ingest summary (offline; newest daily run log)"
# ---------------------------------------------------------------------------
DLOG="$(ls -t "$OUT_DAILY"/run_*.log 2>/dev/null | head -1)"
if [ -z "$DLOG" ]; then
  note "no daily run logs yet"
else
  note "log: ${DLOG#"$DIR"/}"
  staged="$(grep -h 'staged listings:' "$DLOG" 2>/dev/null | tail -1)"
  [ -n "$staged" ] && note "$(printf '%s' "$staged" | sed 's/^[[:space:]]*//')"
  if grep -q 'cre_listings after ingest' "$DLOG" 2>/dev/null; then
    grep -A 12 'cre_listings after ingest' "$DLOG" 2>/dev/null | sed 's/^/        /'
  fi
  if ! tier_loaded daily; then
    note "daily retired/unloaded; skipping legacy daily log sentinel check"
  elif grep -q 'daily update complete' "$DLOG" 2>/dev/null; then
    ok "success sentinel present (daily update complete)"
  else
    warn "no 'daily update complete' sentinel in newest daily log (run may have aborted, or is mid-run)"
  fi
fi

# ---------------------------------------------------------------------------
section "runtime artifacts & lock"
# ---------------------------------------------------------------------------
# Disk footprint: the monitor tier writes a large enumeration artifact every run
# (~100MB at page-cap 60, 8x/day) and the launchd redirect logs are append-only.
# cre_run_tier.sh prunes/caps these; surface the footprint so a runaway is caught
# early, well below real disk pressure.
MON_KB="$(dir_size_kb "$OUT_MONITOR")"; MON_KB="${MON_KB:-0}"
DLY_KB="$(dir_size_kb "$OUT_DAILY")";   DLY_KB="${DLY_KB:-0}"
if [ "${MON_KB:-0}" -gt $(( 8 * 1024 * 1024 )) ]; then        # > 8 GB
  warn "out/monitor footprint $(human_kb "$MON_KB") (large; check monitor-artifact pruning in cre_run_tier.sh)"
else
  note "out/monitor footprint $(human_kb "$MON_KB")"
fi
if [ "${DLY_KB:-0}" -gt $(( 4 * 1024 * 1024 )) ]; then        # > 4 GB
  warn "out/daily footprint $(human_kb "$DLY_KB") (large; check daily prune + launchd-log cap)"
else
  note "out/daily footprint $(human_kb "$DLY_KB")"
fi

# Lock state: a lock held by a live process for far longer than any legitimate
# run is the silent-skip failure mode (every tier then exits 0 doing nothing).
LOCKDIR="$OUT_DAILY/.cre.lock"
if [ -d "$LOCKDIR" ]; then
  lpid=""; lepoch=""
  [ -f "$LOCKDIR/pid" ] && read -r lpid lepoch <"$LOCKDIR/pid" 2>/dev/null || true
  lage=""
  [ -n "${lepoch:-}" ] && lage=$(( now_epoch - lepoch ))
  if [ -n "${lpid:-}" ] && kill -0 "$lpid" 2>/dev/null; then
    if [ -n "$lage" ] && [ "$lage" -gt $(( 18 * 3600 )) ]; then
      warn "lock held by live pid $lpid for $(human_age "$lage") (exceeds 18h; possible hung run)"
      note "if confirmed hung: kill $lpid, then rm -rf \"$LOCKDIR\""
    else
      ok "lock held by live pid $lpid${lage:+ ($(human_age "$lage"))} (a tier is running)"
    fi
  else
    warn "stale lock present (owner '${lpid:-unknown}' not alive); next scheduled run reclaims it"
    note "to clear now: rm -rf \"$LOCKDIR\""
  fi
else
  ok "no lock held (no tier running)"
fi

# ---------------------------------------------------------------------------
section "Firecrawl stack"
# ---------------------------------------------------------------------------
if [ "$FULL_HEALTH" -eq 1 ]; then
  if bash "$FC_DIR/scripts/firecrawl-ops/firecrawl_healthcheck.sh" >/dev/null 2>&1; then
    ok "full healthcheck passed (docker + API + scrape smoke)"
  else
    bad "full healthcheck FAILED (collect/monitor cannot run until the stack is up)"
  fi
elif curl -fsS --max-time 5 "$API_URL/" >/dev/null 2>&1; then
  ok "API reachable at $API_URL (use --full-health for the full scrape smoke test)"
else
  bad "API not reachable at $API_URL (is the Docker stack up? cd \"$FC_DIR\" && docker compose up -d)"
fi

# ---------------------------------------------------------------------------
section "environment"
# ---------------------------------------------------------------------------
case "$DIR" in
  "$HOME"/Documents/*)
    warn "clone under ~/Documents (macOS TCC): scheduled launchd runs exit 126 until fixed"
    note "fix: relocate the clone outside ~/Documents, or grant /bin/bash Full Disk Access (see SETUP.md)" ;;
  *)
    ok "clone outside ~/Documents (no TCC blocker for launchd)" ;;
esac
# Mirror what the scheduled tiers actually use. An interactive shell has no
# CRE_ENV_FILE, so load_db_url() would fall back to the ~/Documents default and
# misreport the source. Pull CRE_ENV_FILE from the installed plist (read-only)
# so the heartbeat reflects the same non-TCC env file the launchd runs resolve.
env_src="interactive default"
if [ -z "${CRE_ENV_FILE:-}" ]; then
  for _plist in "$HOME/Library/LaunchAgents/ai.agentic.cre-monitor.plist" \
                "$HOME/Library/LaunchAgents/ai.agentic.cre-enrich.plist" \
                "$HOME/Library/LaunchAgents/ai.agentic.cre-weekly.plist" \
                "$HOME/Library/LaunchAgents/ai.agentic.cre-daily.plist"; do
    [ -f "$_plist" ] || continue
    _cef="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:CRE_ENV_FILE' "$_plist" 2>/dev/null || true)"
    if [ -n "$_cef" ]; then
      export CRE_ENV_FILE="$_cef"
      env_src="launchd plist CRE_ENV_FILE"
      break
    fi
  done
else
  env_src="shell CRE_ENV_FILE"
fi
ENVP="$(python3 - <<'PY' 2>/dev/null
import contextlib, io
import cre_ingest
try:
    with contextlib.redirect_stderr(io.StringIO()):
        _u, path = cre_ingest.load_db_url(None)
    print("OK\t" + path)   # path only; the URL value is never printed
except SystemExit:
    print("MISSING")
except Exception as exc:  # noqa: BLE001
    print("ERROR\t" + type(exc).__name__)
PY
)"
case "$ENVP" in
  OK*)     ok "POSTGRES_URL env file found at $(printf '%s' "$ENVP" | cut -f2-) (via $env_src)" ;;
  MISSING) warn "no POSTGRES_URL env file found (ingest will fail until CRE_ENV_FILE is set)" ;;
  *)       warn "env discovery error: $ENVP" ;;
esac

# ---------------------------------------------------------------------------
section "recent launchd stderr (newest tail)"
# ---------------------------------------------------------------------------
shown=0
for tier in monitor enrich daily weekly; do
  errlog="$OUT_DAILY/cre-$tier.err.log"
  [ -s "$errlog" ] || continue
  shown=1
  tailout="$(tail -3 "$errlog" 2>/dev/null)"
  if printf '%s' "$tailout" | grep -q 'Operation not permitted'; then
    warn "cre-$tier.err.log shows 'Operation not permitted' (TCC-126 signature)"
  fi
  printf '  --- cre-%s.err.log (last 3 lines) ---\n' "$tier"
  printf '%s\n' "$tailout" | sed 's/^/        /'
done
[ "$shown" -eq 0 ] && note "no non-empty launchd stderr logs"

# ---------------------------------------------------------------------------
section "disappearance-only signal staleness"
# ---------------------------------------------------------------------------
# Sources with no native status field rely on vanishing from a full sweep for
# their sold-signal, which the weekly mark-missing reconciliation detects. If
# the monitor has not enumerated these sources recently the sold-signal is stale
# and the board will silently carry rows that may already be sold.
#
# This check uses the local monitor artifacts under out/monitor/ as the
# read-only proxy for cre_source_index.last_enumerated_at: the monitor writes
# the artifact at the same time it updates cre_source_index, so the artifact
# mtime is equivalent to last_enumerated_at without requiring a DB connection.
# A DB-backed exact read is explicitly deferred to keep cre_status.sh no-DB.
#
# Threshold: 8 days, conservatively longer than the 7-day weekly cadence so a
# normal weekly sweep does not trip the alarm.
SIGNAL_STALE_SECS=$(( 8 * 86400 ))

# Source of truth is cre_ingest.STATUS_SOURCE_PATHS: an empty per-source path
# list means no native terminal status signal, so disappearance is the only
# status signal. Fallback matches the current ingestor contract.
DISAPPEAR_ONLY_SOURCES="$(python3 - <<'PY' 2>/dev/null
import cre_ingest

print(" ".join(sorted(
    key for key, paths in cre_ingest.STATUS_SOURCE_PATHS.items() if not paths
)))
PY
)"
[ -n "$DISAPPEAR_ONLY_SOURCES" ] || DISAPPEAR_ONLY_SOURCES="avison-young cbre jll marcus-millichap newmark savills transwestern"

newest_mon_art() {
  # Return path of the newest out/monitor/monitor_*.json file (may be empty).
  ls -t "$OUT_MONITOR"/monitor_*.json 2>/dev/null | head -1
}

# Find newest monitor artifact that mentions a given source key with grouped > 0.
# Uses light grep: looks for the source key string in the artifact content.
# Acceptable fallback: an artifact that mentions the key at all signals enumeration.
source_last_seen_artifact() {
  local sk="$1"
  ls -t "$OUT_MONITOR"/monitor_*.json 2>/dev/null | while read -r f; do
    # Check if the file mentions this source key under by_source with grouped > 0.
    # We look for "grouped":[^0}] after the source key to find a nonzero grouped count.
    # Because exact JSON parsing is not available, check for the key and grouped presence.
    if grep -q "\"$sk\"" "$f" 2>/dev/null; then
      # Attempt to find a grouped > 0 signal: look for "grouped":N where N is nonzero.
      # The by_source block for a source looks like: "cbre":{"enumerated_flat":N,"grouped":N,...}
      # We extract the block and check for a nonzero grouped value.
      local block
      block="$(grep -o "\"$sk\":{[^}]*}" "$f" 2>/dev/null | head -1)"
      if [ -n "$block" ]; then
        local grp
        grp="$(printf '%s' "$block" | grep -o '"grouped":[0-9]*' | grep -o '[0-9]*$')"
        if [ -n "$grp" ] && [ "$grp" -gt 0 ] 2>/dev/null; then
          echo "$f"
          return 0
        fi
      fi
      # Fallback: if we cannot parse grouped but the key is present, count the file.
      # This avoids false "never seen" when the JSON shape is unexpected.
      echo "$f"
      return 0
    fi
  done
}

# Check for any monitor artifact at all (coarse staleness signal).
NEWEST_MON="$(newest_mon_art)"

if [ -z "$NEWEST_MON" ]; then
  # No monitor artifacts on this clone yet (fresh setup or artifacts pruned).
  # Note, do not warn: a brand-new setup has never run the monitor.
  note "no monitor artifacts found under out/monitor/ (monitor has never run on this clone)"
  note "disappearance-only sources rely on the monitor sweep for their sold-signal:"
  for sk in $DISAPPEAR_ONLY_SOURCES; do
    note "  $sk"
  done
else
  # Check each disappearance-only source individually.
  for sk in $DISAPPEAR_ONLY_SOURCES; do
    seen_file="$(source_last_seen_artifact "$sk")"
    if [ -z "$seen_file" ]; then
      # Source was never seen in any monitor artifact.
      NEWEST_AGE=$(( now_epoch - $(file_mtime "$NEWEST_MON") ))
      if [ "$NEWEST_AGE" -gt "$SIGNAL_STALE_SECS" ]; then
        warn "$sk: never enumerated in any monitor artifact and newest artifact is $(human_age "$NEWEST_AGE") old (sold-signal is stale)"
      else
        note "$sk: not found in monitor artifacts yet (sold-signal will be available once the monitor has enumerated it)"
      fi
    else
      seen_mtime="$(file_mtime "$seen_file")"
      seen_age=$(( now_epoch - ${seen_mtime:-now_epoch} ))
      if [ "$seen_age" -gt "$SIGNAL_STALE_SECS" ]; then
        warn "$sk: last enumerated $(human_age "$seen_age") ago (threshold: $(human_age "$SIGNAL_STALE_SECS")); sold-signal is stale"
        note "  last artifact with $sk: ${seen_file#"$DIR"/}"
      else
        ok "$sk: enumerated $(human_age "$seen_age") ago (within $(human_age "$SIGNAL_STALE_SECS") threshold)"
      fi
    fi
  done
fi

# ---------------------------------------------------------------------------
section "summary"
# ---------------------------------------------------------------------------
if [ "$PROBLEMS" -eq 0 ]; then
  printf '  healthy: no problems detected\n'
  exit 0
fi
printf '  %d problem(s) detected; see WARN/FAIL above\n' "$PROBLEMS"
exit 1
