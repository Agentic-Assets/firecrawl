#!/usr/bin/env bash
# =============================================================================
# cre_daily_update.sh: daily CRE listing refresh (collect -> ingest).
#
# Collects every supported source (sale + lease, full pagination) through the
# local self-hosted Firecrawl API, then upserts into the Supabase `credeals`
# schema. By default it includes full-run reconciliation (--mark-missing
# soft-deletes listings a clean full pass no longer sees). Use
# --no-mark-missing whenever any source is blocked or under investigation.
#
# Usage:
#   bash cre_daily_update.sh --no-mark-missing   # safe daily additive run
#   bash cre_daily_update.sh                     # full run with reconciliation
#   CRE_SOURCES=svn,cbre bash cre_daily_update.sh   # subset of sources
#
# Requirements (fresh clone: run `bash cre_setup.sh` first; see SETUP.md):
#   - Local Firecrawl stack up (http://localhost:3002); checked before running.
#   - npm deps installed in this directory (npx tsx collect.ts).
#   - psql (Homebrew libpq) + an env file with POSTGRES_URL_NON_POOLING.
#     Discovery: --env-file > $CRE_ENV_FILE > ~/Documents defaults; never commit.
#
# Scheduling (launchd): render + install the tiers with
#   bash launchd/install_launchd.sh all   (gated; add --load to load). Logs land
#   in ./out/daily/. See launchd/README.md.
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

STAMP="$(date +%Y-%m-%d_%H%M)"
OUT_DIR="$DIR/out/daily"
mkdir -p "$OUT_DIR"
RUN_JSON="$OUT_DIR/run_$STAMP.json"
LOG="$OUT_DIR/run_$STAMP.log"

# Name the phase currently running so an unguarded failure under `set -e` says
# WHERE it died instead of failing silently. STEP is advanced before each phase;
# guarded checks (|| {...}, || rc=$?) intentionally do not trip this trap.
STEP="startup"
trap 'rc=$?; echo "[daily] FAILED at step: ${STEP} (rc=${rc}); see ${LOG}" | tee -a "${LOG}" >&2' ERR

SOURCES="${CRE_SOURCES:-all}"
CONCURRENCY="${CRE_CONCURRENCY:-3}"
PAGE_CAP="${CRE_PAGE_CAP:-400}"
MARK_MISSING="--mark-missing"
for arg in "$@"; do
  [ "$arg" = "--no-mark-missing" ] && MARK_MISSING=""
done

# Keep only the newest $2 files matching glob $1 in OUT_DIR; delete the rest.
# Space-safe: globs into a bash array (no xargs word-splitting) and tolerates
# spaces in the clone path. Relies on these artifact names never containing
# newlines (they are date-stamped). The run_*.json glob never matches the
# last_run_<tier>.json verdict markers. No-op when <= $2 files match. The
# count guard keeps the array non-empty before "${matches[@]}" (bash 3.2 + set -u).
prune_keep() {
  local pattern="$1" keep="$2"
  local -a matches=()
  local f
  shopt -s nullglob
  for f in "$OUT_DIR"/$pattern; do matches+=("$f"); done
  shopt -u nullglob
  [ "${#matches[@]}" -le "$keep" ] && return 0
  local path
  for f in "${matches[@]}"; do
    printf '%s\t%s\n' "$(stat -f '%m' "$f" 2>/dev/null || stat -c '%Y' "$f" 2>/dev/null || echo 0)" "$f"
  done | sort -rn | tail -n "+$((keep + 1))" | cut -f2- | while IFS= read -r path; do
    if [ -n "$path" ]; then rm -f "$path"; fi
  done
}

prune_artifacts() {
  # Keep the last N daily artifacts; raw runs are large. The run_*.json glob
  # never matches the last_run_<tier>.json verdict markers.
  prune_keep 'run_*.json'  14
  prune_keep 'run_*.log'   29
  prune_keep 'gate_*.json' 14
}
# Prune on EVERY exit (success or failure) so disk does not grow fastest exactly
# when the pipeline is broken (e.g. a multi-day DB outage). The current run's
# artifacts are the newest and are retained for debugging. Clear the ERR trap
# first so prune's own internal nonzero statuses cannot trip the step logger.
trap 'trap - ERR; prune_artifacts >/dev/null 2>&1 || true' EXIT

STEP="healthcheck"
echo "[1/4] healthcheck" | tee -a "$LOG"
FC_DIR="${FC_DIR:-$DIR/../../..}"
bash "$FC_DIR/scripts/firecrawl-ops/firecrawl_healthcheck.sh" >>"$LOG" 2>&1 || {
  echo "local Firecrawl stack unhealthy; aborting (see $LOG)" | tee -a "$LOG"
  exit 1
}

STEP="collect"
echo "[2/4] collect: sources=$SOURCES (sale+lease, unlimited)" | tee -a "$LOG"
npx tsx collect.ts \
  --source="$SOURCES" \
  --transaction=both \
  --max-items=0 \
  --page-cap="$PAGE_CAP" \
  --concurrency="$CONCURRENCY" \
  --out="$RUN_JSON" >>"$LOG" 2>&1

STEP="coverage-gate"
echo "[3/4] coverage gate (observe-only; advisory mark-missing fail-safe)" | tee -a "$LOG"
GATE_JSON="$OUT_DIR/gate_$STAMP.json"
# Observe-only: --apply reads cre_source_baseline (no --update-baseline => no DB
# writes); --strict exits nonzero if any source's coverage is a 'hold' (partial
# or regressed enumeration). When mark-missing is requested, a hold downgrades
# this run to additive so a gappy pass can never soft-delete live listings.
# cre_ingest.py keeps its own folded-coverage + floor guard as a second layer.
GATE_RC=0
python3 cre_gate.py --in "$RUN_JSON" --apply --strict --out "$GATE_JSON" >>"$LOG" 2>&1 || GATE_RC=$?
if [ -n "$MARK_MISSING" ] && [ "$GATE_RC" -ne 0 ]; then
  echo "  coverage gate held (rc=$GATE_RC); downgrading to --no-mark-missing this run" | tee -a "$LOG"
  MARK_MISSING=""
fi

STEP="ingest"
echo "[4/4] ingest -> credeals" | tee -a "$LOG"
# shellcheck disable=SC2086
# Status activation stays OFF here (cre_ingest.py default). The daily refresh
# updates listing data without flipping board state; activate deliberately with
# CRE_ACTIVATE_STATUS=1 only after the EQUIRE consumer board-gate is deployed.
python3 cre_ingest.py --in "$RUN_JSON" $MARK_MISSING >>"$LOG" 2>&1

STEP="done"
echo "daily update complete: $RUN_JSON" | tee -a "$LOG"
tail -12 "$LOG" | grep -A 12 "cre_listings after ingest" || true
# Pruning runs in the EXIT trap (prune_artifacts) so disk is bounded on every
# invocation, including failed runs that abort before reaching this point.
