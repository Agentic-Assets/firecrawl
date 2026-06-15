#!/usr/bin/env bash
# =============================================================================
# cre_status.sh: read-only health / heartbeat for the CRE collector.
#
# One command that answers "is the scheduled pipeline alive and healthy?" so an
# operator (or a coding agent on the Mac mini) does not have to cross-reference
# `launchctl list`, log tails, and per-run markers by hand. It reports, per
# tier (monitor / daily / weekly):
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

# 1.5x the nominal cadence: monitor every 3h, daily 24h, weekly 7d.
stale_threshold() {
  case "$1" in
    monitor) echo $(( 4 * 3600 + 1800 )) ;;   # 4.5h
    daily)   echo $(( 36 * 3600 )) ;;          # 36h
    weekly)  echo $(( 10 * 86400 )) ;;         # 10d
  esac
}

newest_artifact() {
  case "$1" in
    monitor)      ls -t "$OUT_MONITOR"/monitor_*.json 2>/dev/null | head -1 ;;
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
for tier in monitor daily weekly; do
  label="ai.agentic.cre-$tier"
  line="$(printf '%s\n' "$LCTL" | awk -v l="$label" '$3==l {print; exit}')"
  if [ -z "$line" ]; then
    if [ "$tier" = "weekly" ]; then
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
for tier in monitor daily weekly; do
  marker="$OUT_DAILY/last_run_$tier.json"
  verdict=""
  if [ -f "$marker" ]; then
    verdict="rc=$(marker_field "$marker" rc) ok=$(marker_field "$marker" ok) end=$(marker_field "$marker" end)"
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
  if [ "$age" -gt "$thr" ]; then
    if [ "$tier" = "weekly" ]; then
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
  if grep -q 'daily update complete' "$DLOG" 2>/dev/null; then
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
  OK*)     ok "POSTGRES_URL env file found at $(printf '%s' "$ENVP" | cut -f2-)" ;;
  MISSING) warn "no POSTGRES_URL env file found (ingest will fail until CRE_ENV_FILE is set)" ;;
  *)       warn "env discovery error: $ENVP" ;;
esac

# ---------------------------------------------------------------------------
section "recent launchd stderr (newest tail)"
# ---------------------------------------------------------------------------
shown=0
for tier in monitor daily weekly; do
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
section "summary"
# ---------------------------------------------------------------------------
if [ "$PROBLEMS" -eq 0 ]; then
  printf '  healthy: no problems detected\n'
  exit 0
fi
printf '  %d problem(s) detected; see WARN/FAIL above\n' "$PROBLEMS"
exit 1
