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
# Requirements:
#   - Local Firecrawl stack up (http://localhost:3002); checked before running.
#   - npm deps installed in this directory (npx tsx collect.ts).
#   - psql (Homebrew libpq) + an env file with POSTGRES_URL_NON_POOLING
#     (default: dynamically-display-cre-listing-data/.env.local; never commit).
#
# Scheduling (launchd example, runs 06:30 daily):
#   create ~/Library/LaunchAgents/com.agenticassets.cre-daily.plist pointing
#   ProgramArguments at this script, then `launchctl load` it. Logs land in
#   ./out/daily/.
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

STAMP="$(date +%Y-%m-%d_%H%M)"
OUT_DIR="$DIR/out/daily"
mkdir -p "$OUT_DIR"
RUN_JSON="$OUT_DIR/run_$STAMP.json"
LOG="$OUT_DIR/run_$STAMP.log"

SOURCES="${CRE_SOURCES:-all}"
CONCURRENCY="${CRE_CONCURRENCY:-3}"
PAGE_CAP="${CRE_PAGE_CAP:-400}"
MARK_MISSING="--mark-missing"
for arg in "$@"; do
  [ "$arg" = "--no-mark-missing" ] && MARK_MISSING=""
done

echo "[1/3] healthcheck" | tee -a "$LOG"
FC_DIR="${FC_DIR:-$DIR/../../..}"
bash "$FC_DIR/scripts/firecrawl-ops/firecrawl_healthcheck.sh" >>"$LOG" 2>&1 || {
  echo "local Firecrawl stack unhealthy; aborting (see $LOG)" | tee -a "$LOG"
  exit 1
}

echo "[2/3] collect: sources=$SOURCES (sale+lease, unlimited)" | tee -a "$LOG"
npx tsx collect.ts \
  --source="$SOURCES" \
  --transaction=both \
  --max-items=0 \
  --page-cap="$PAGE_CAP" \
  --concurrency="$CONCURRENCY" \
  --out="$RUN_JSON" >>"$LOG" 2>&1

echo "[3/3] ingest -> credeals" | tee -a "$LOG"
# shellcheck disable=SC2086
python3 cre_ingest.py --in "$RUN_JSON" $MARK_MISSING >>"$LOG" 2>&1

# Keep the last 14 daily artifacts; raw runs are large.
ls -t "$OUT_DIR"/run_*.json 2>/dev/null | tail -n +15 | xargs rm -f 2>/dev/null || true
ls -t "$OUT_DIR"/run_*.log 2>/dev/null | tail -n +30 | xargs rm -f 2>/dev/null || true

echo "daily update complete: $RUN_JSON" | tee -a "$LOG"
tail -12 "$LOG" | grep -A 12 "cre_listings after ingest" || true
