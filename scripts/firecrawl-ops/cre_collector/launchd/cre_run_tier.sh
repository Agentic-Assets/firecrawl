#!/usr/bin/env bash
# cre_run_tier.sh — flock-serialized dispatcher for the three CRE launchd tiers.
#
# Usage: cre_run_tier.sh <monitor|daily|weekly>
#
# All three tiers share one exclusive lock so they cannot overlap with each
# other or with any manual run that acquires the same lockfile.  If the lock
# is already held the script exits silently (exit 0) — launchd will retry on
# the next scheduled interval.
#
# Tier semantics:
#   monitor  — cheap enumeration diff (cre_monitor.py); NOT YET IMPLEMENTED
#   daily    — full collect + additive ingest with --no-mark-missing (safe default)
#   weekly   — full collect + ingest with --mark-missing (ONLY tier permitted to
#              soft-delete rows; runs after proven convergence on Tier-1 sources)

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COLLECTOR_DIR="/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector"
LOCKFILE="${COLLECTOR_DIR}/out/daily/.cre.lock"
MONITOR_SCRIPT="${COLLECTOR_DIR}/cre_monitor.py"
DAILY_SCRIPT="${COLLECTOR_DIR}/cre_daily_update.sh"

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
TIER="${1:-}"
case "${TIER}" in
    monitor|daily|weekly) ;;
    *)
        echo "[cre_run_tier] ERROR: first argument must be monitor, daily, or weekly (got: '${TIER}')" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Acquire exclusive lock (non-blocking).  Exit silently if already locked.
# ---------------------------------------------------------------------------
exec 9>"${LOCKFILE}"
if ! flock -n 9; then
    echo "[cre_run_tier] Another CRE tier is running — skipping ${TIER} at $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Move into the collector directory so relative paths inside child scripts
# resolve correctly.
# ---------------------------------------------------------------------------
cd "${COLLECTOR_DIR}"

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

echo "[cre_run_tier] START tier=${TIER} at $(ts)"

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${TIER}" in

    monitor)
        if [[ ! -f "${MONITOR_SCRIPT}" ]]; then
            echo "[cre_run_tier] NOT YET IMPLEMENTED: ${MONITOR_SCRIPT} does not exist." >&2
            echo "[cre_run_tier] The monitor tier will run once cre_monitor.py is built (design-doc section 10 Phase 3)." >&2
            echo "[cre_run_tier] Exiting cleanly — no data was modified." >&2
            exit 0
        fi
        echo "[cre_run_tier] Running monitor pipeline: python3 ${MONITOR_SCRIPT}"
        python3 "${MONITOR_SCRIPT}"
        ;;

    daily)
        echo "[cre_run_tier] Running daily full collect + additive ingest (--no-mark-missing)"
        bash "${DAILY_SCRIPT}" --no-mark-missing
        ;;

    weekly)
        echo "[cre_run_tier] Running weekly full collect + reconcile (--mark-missing ENABLED)"
        echo "[cre_run_tier] WARNING: this is the ONLY tier permitted to soft-delete rows."
        bash "${DAILY_SCRIPT}" --mark-missing
        ;;

esac

echo "[cre_run_tier] END   tier=${TIER} at $(ts)"
