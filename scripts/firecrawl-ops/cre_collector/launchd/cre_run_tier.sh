#!/usr/bin/env bash
# cre_run_tier.sh: worker for the governed CRE launchd tier dispatcher.
#
# Usage: cre_run_tier.sh <monitor|enrich|weekly|daily>
#
# The public entrypoint delegates lock lifetime to cre_tier_dispatch.py. That
# Python process holds the persistent SharedLock authority and recovery-sync
# flocks for the full worker lifetime, so scheduled/manual tiers cannot overlap
# a governed quarantine archive. This shell retains only tier work, logs, and
# verdict markers. Each real run writes out/daily/last_run_<tier>.json for
# cre_status.sh to read.
#
# Tier semantics:
#   monitor  : cheap enumeration diff: runs collect.ts --monitor then cre_monitor.py
#              observe-only by default; pass CRE_MONITOR_APPLY=1 to enable --apply
#              (GATED: requires explicit go-ahead before enabling --apply or loading the plist)
#   enrich   : drain cre_enrichment_queue: runs cre_enrich.py to claim a batch of
#              monitor-flagged new/changed listings, scrape only those detail
#              pages, and re-ingest ADDITIVELY (cre_ingest.py --in). Never passes
#              --mark-missing nor --activate-status; cannot soft-delete or flip
#              board state. Batch size from CRE_ENRICH_BATCH (default 200).
#   weekly   : full collect + ingest backstop. ADDITIVE by default
#              (--no-mark-missing); soft-delete (--mark-missing) fires ONLY when
#              CRE_WEEKLY_MARK_MISSING=1 is set in this tier's environment. The
#              weekly tier is the ONLY tier permitted to soft-delete rows, and
#              only under that explicit escalation (still triple-gated downstream
#              by cre_gate.py --strict and per-brokerage ingest eligibility).
#   daily    : RETIRED. Replaced by monitor (2x/day) + enrich (every 4h). The
#              case is kept for rollback only; the plist is no longer scheduled.
#              Runs cre_daily_update.sh --no-mark-missing (additive).

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Self-locate the collector dir from this script's own path (launchd/..), so the
# runner is portable across machines and clone locations. launchd plists still
# need absolute paths; generate them per-machine with install_launchd.sh.
COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAILY_OUT_DIR="${COLLECTOR_DIR}/out/daily"
MONITOR_SCRIPT="${COLLECTOR_DIR}/cre_monitor.py"
ENRICH_SCRIPT="${COLLECTOR_DIR}/cre_enrich.py"
DAILY_SCRIPT="${COLLECTOR_DIR}/cre_daily_update.sh"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# Public/manual launchd entrypoints always go through the Python authority
# owner.  The private worker mode is accepted only after it proves it inherited
# both descriptor-bound flocks from that owner; a direct --already-locked call
# therefore cannot bypass the canonical quarantine guard.
if [ "${1:-}" != "--already-locked" ]; then
    exec python3 "${COLLECTOR_DIR}/cre_tier_dispatch.py" "$@"
fi
shift
if ! python3 "${COLLECTOR_DIR}/cre_tier_dispatch.py" --verify-inherited-lock; then
    echo "[cre_run_tier] ERROR: governed inherited lock proof failed" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
TIER="${1:-}"
case "${TIER}" in
    monitor|enrich|weekly|daily) ;;
    *)
        echo "[cre_run_tier] ERROR: first argument must be monitor, enrich, weekly, or daily (got: '${TIER}')" >&2
        exit 1
        ;;
esac

# Ensure the shared artifact dir exists before writing run markers. The Python
# dispatcher has already acquired the canonical authority before this worker
# can reach this point.
mkdir -p "${DAILY_OUT_DIR}"

RUN_START="$(ts)"
MARKER="${DAILY_OUT_DIR}/last_run_${TIER}.json"

previous_failure_count() {
    # Markers are deliberately tiny JSON files, so keep this dependency-free.
    # A missing or malformed prior marker is treated as no prior failure.
    local prior=""
    [ -f "${MARKER}" ] || { printf '0\n'; return 0; }
    prior="$(grep -o '"consecutive_failures":[0-9][0-9]*' "${MARKER}" 2>/dev/null | head -1 | cut -d: -f2 || true)"
    case "${prior}" in
        ''|*[!0-9]*) printf '0\n' ;;
        *) printf '%s\n' "${prior}" ;;
    esac
}

read_alert_webhook_url() {
    # Scheduled jobs receive only the path to the secret. The URL itself stays
    # outside git and out of the rendered plist. Direct CRE_ALERT_WEBHOOK_URL is
    # retained for supervised/manual runs.
    local direct="${CRE_ALERT_WEBHOOK_URL:-}" secret_file="${CRE_ALERT_WEBHOOK_FILE:-}"
    local mode="" value=""
    if [ -n "${direct}" ]; then
        printf '%s' "${direct}"
        return 0
    fi
    [ -n "${secret_file}" ] || return 0
    if [ ! -f "${secret_file}" ] || [ ! -r "${secret_file}" ] || [ ! -O "${secret_file}" ]; then
        echo "[cre_run_tier] ALERT not configured: CRE_ALERT_WEBHOOK_FILE must be an owned, readable regular file" >&2
        return 0
    fi
    mode="$(stat -f '%Lp' "${secret_file}" 2>/dev/null || stat -c '%a' "${secret_file}" 2>/dev/null || true)"
    case "${mode}" in
        400|600) ;;
        *)
            echo "[cre_run_tier] ALERT not configured: CRE_ALERT_WEBHOOK_FILE permissions must be 400 or 600" >&2
            return 0
            ;;
    esac
    value="$(<"${secret_file}")"
    printf '%s' "${value}"
}

notify_failure() {
    # Optional and best-effort. A missing webhook must never mask the real tier
    # exit code or turn a local outage into an alerting outage.
    local rc="$1" failures="$2" url=""
    url="$(read_alert_webhook_url)"
    [ -n "${url}" ] || {
        echo "[cre_run_tier] ALERT not sent: no valid webhook is configured (tier=${TIER} rc=${rc} consecutive_failures=${failures})" >&2
        return 0
    }
    if ! command -v curl >/dev/null 2>&1; then
        echo "[cre_run_tier] ALERT not sent: curl is unavailable (tier=${TIER})" >&2
        return 0
    fi
    if [[ "${url}" == *$'\n'* || "${url}" == *$'\r'* ]]; then
        echo "[cre_run_tier] ALERT not sent: webhook contains a newline (tier=${TIER})" >&2
        return 0
    fi
    # TIER is a fixed enum and the values are numeric, so this JSON cannot carry
    # unescaped caller input. Pass the credential through curl's stdin config,
    # not argv, so it cannot appear in process listings. Escape the only config
    # delimiters that a valid URL can contain. Run synchronously with a bounded
    # timeout so launchd cannot tear down the job before delivery completes.
    local config_url="${url//\\/\\\\}"
    config_url="${config_url//\"/\\\"}"
    printf 'url = "%s"\n' "${config_url}" | curl --config - \
        --silent --show-error --fail --max-time 10 \
        -H 'Content-Type: application/json' \
        -d "{\"text\":\"CRE collector tier ${TIER} failed (rc=${rc}, consecutive failures=${failures}). See cre_status.sh and the tier stderr log.\"}" \
        >/dev/null 2>&1 || \
        echo "[cre_run_tier] ALERT delivery failed (tier=${TIER}); preserving tier rc=${rc}" >&2
}

write_marker() {
    # Persist a tiny machine-readable verdict so cre_status.sh (and a coding
    # agent on the Mac mini) can tell success from failure without scraping
    # log tails. Best-effort: never let marker IO fail the run.
    local rc="$1" okflag="false" failures=0 prior=0 tmp="${MARKER}.tmp.$$"
    [ "${rc}" = "0" ] && okflag="true"
    prior="$(previous_failure_count)"
    if [ "${rc}" != "0" ]; then
        failures=$(( prior + 1 ))
    fi
    { cat >"${tmp}" <<EOF
{"tier":"${TIER}","start":"${RUN_START}","end":"$(ts)","rc":${rc},"ok":${okflag},"consecutive_failures":${failures}}
EOF
      mv -f "${tmp}" "${MARKER}"
    } 2>/dev/null || rm -f "${tmp}" 2>/dev/null || true
    printf '%s\n' "${failures}"
}

# --- disk maintenance (runs in finish() on every real run) -----------------
# Keep the newest $3 files matching glob $2 in dir $1; space-safe; no-op when
# few match. Mirrors cre_daily_update.sh prune_keep, for out/monitor/ artifacts.
_keep_newest() {
    local dir="$1" pattern="$2" keep="$3"
    [ -d "${dir}" ] || return 0
    local -a m=()
    local f
    shopt -s nullglob
    for f in "${dir}"/$pattern; do m+=("$f"); done
    shopt -u nullglob
    [ "${#m[@]}" -le "${keep}" ] && return 0
    local path
    for f in "${m[@]}"; do
        printf '%s\t%s\n' "$(stat -f '%m' "$f" 2>/dev/null || stat -c '%Y' "$f" 2>/dev/null || echo 0)" "$f"
    done | sort -rn | tail -n "+$(( keep + 1 ))" | cut -f2- | while IFS= read -r path; do
        if [ -n "${path}" ]; then rm -f "${path}"; fi
    done
}

# Trim an append-only log to its last half when it exceeds $2 bytes. The running
# launchd job keeps its own fd open, so this takes effect from the next fire on.
_cap_log() {
    local f="$1" max="$2" size tmp
    [ -f "${f}" ] || return 0
    size="$(stat -f '%z' "${f}" 2>/dev/null || stat -c '%s' "${f}" 2>/dev/null || echo 0)"
    [ "${size}" -gt "${max}" ] || return 0
    tmp="${f}.trim.$$"
    if tail -c "$(( max / 2 ))" "${f}" >"${tmp}" 2>/dev/null; then
        mv -f "${tmp}" "${f}" 2>/dev/null || rm -f "${tmp}" 2>/dev/null || true
    else
        rm -f "${tmp}" 2>/dev/null || true
    fi
}

prune_runtime_artifacts() {
    # Monitor enumeration artifacts (~100MB each, 8/day) would fill the disk in
    # weeks; keep ~3 days. Keep the per-run monitor logs bounded too, and cap the
    # append-only launchd redirect logs. Best-effort; never fail the run.
    {
        _keep_newest "${COLLECTOR_DIR}/out/monitor" 'monitor_*.json' 24
        _keep_newest "${COLLECTOR_DIR}/out/monitor" 'monitor_*.log'  24
        local L
        for L in cre-monitor cre-enrich cre-weekly cre-daily; do
            _cap_log "${DAILY_OUT_DIR}/${L}.out.log" 10485760   # 10 MB
            _cap_log "${DAILY_OUT_DIR}/${L}.err.log" 10485760
        done
    } 2>/dev/null || true
}

finish() {
    local rc=$?
    local failures
    failures="$(write_marker "${rc}")"
    if [ "${rc}" != "0" ]; then
        notify_failure "${rc}" "${failures:-1}"
    fi
    prune_runtime_artifacts            # bound disk on every real run, pass or fail
    exit "${rc}"
}
trap finish EXIT

# This test-only no-op runs only after descriptor-bound ownership proof. It
# makes the shell/dispatcher handoff testable without starting a collection.
if [ "${CRE_TIER_LOCK_VERIFY_ONLY:-0}" = "1" ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Move into the collector directory so relative paths inside child scripts
# resolve correctly.
# ---------------------------------------------------------------------------
cd "${COLLECTOR_DIR}"

echo "[cre_run_tier] START tier=${TIER} at ${RUN_START}"

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${TIER}" in

    monitor)
        MONITOR_STAMP="$(date +%Y-%m-%d_%H%M)"
        MONITOR_OUT_DIR="${COLLECTOR_DIR}/out/monitor"
        mkdir -p "${MONITOR_OUT_DIR}"
        MONITOR_ARTIFACT="${MONITOR_OUT_DIR}/monitor_${MONITOR_STAMP}.json"
        MONITOR_LOG="${MONITOR_OUT_DIR}/monitor_${MONITOR_STAMP}.log"

        # Heavy child output goes to a per-run, pruned log (not the append-only
        # launchd redirect file, which would otherwise grow unbounded 8x/day).
        echo "[cre_run_tier] [1/2] collect --monitor -> ${MONITOR_ARTIFACT} (log: ${MONITOR_LOG})"
        npx tsx collect.ts \
            --source=all \
            --monitor \
            --out="${MONITOR_ARTIFACT}" >>"${MONITOR_LOG}" 2>&1

        APPLY_FLAG=""
        if [ "${CRE_MONITOR_APPLY:-0}" = "1" ]; then
            APPLY_FLAG="--apply"
        fi
        echo "[cre_run_tier] [2/2] cre_monitor.py --in ${MONITOR_ARTIFACT} ${APPLY_FLAG:-(observe-only; CRE_MONITOR_APPLY not set)}"
        # shellcheck disable=SC2086
        python3 "${MONITOR_SCRIPT}" --in "${MONITOR_ARTIFACT}" ${APPLY_FLAG} >>"${MONITOR_LOG}" 2>&1
        ;;

    enrich)
        echo "[cre_run_tier] Running enrich queue worker (additive; --in only, never --mark-missing/--activate-status)"
        ENRICH_SOURCE_ARGS=()
        if [ -n "${CRE_ENRICH_SOURCE:-}" ]; then
            ENRICH_SOURCE_ARGS=(--source "${CRE_ENRICH_SOURCE}")
        fi
        python3 "${ENRICH_SCRIPT}" \
            --batch "${CRE_ENRICH_BATCH:-200}" \
            "${ENRICH_SOURCE_ARGS[@]}"
        ;;

    weekly)
        # Additive by default. Soft-delete (--mark-missing) is gated behind the
        # explicit CRE_WEEKLY_MARK_MISSING=1 escalation; even when set it stays
        # triple-gated downstream (cre_gate.py --strict auto-downgrade +
        # per-brokerage ingest eligibility). Default keeps weekly safe to load.
        MM="--no-mark-missing"
        [ "${CRE_WEEKLY_MARK_MISSING:-0}" = "1" ] && MM="--mark-missing"
        if [ "${MM}" = "--mark-missing" ]; then
            echo "[cre_run_tier] Running weekly full collect + reconcile (--mark-missing ENABLED via CRE_WEEKLY_MARK_MISSING=1)"
            echo "[cre_run_tier] WARNING: this is the ONLY tier permitted to soft-delete rows."
        else
            echo "[cre_run_tier] Running weekly full collect + additive ingest (--no-mark-missing; CRE_WEEKLY_MARK_MISSING not set)"
        fi
        bash "${DAILY_SCRIPT}" "${MM}"
        ;;

    daily)
        # RETIRED: replaced by monitor (2x/day) + enrich (every 4h). Kept for
        # rollback only; the daily plist is no longer scheduled.
        echo "[cre_run_tier] Running daily full collect + additive ingest (--no-mark-missing) [RETIRED tier, rollback only]"
        bash "${DAILY_SCRIPT}" --no-mark-missing
        ;;

esac

echo "[cre_run_tier] END   tier=${TIER} at $(ts)"
