#!/usr/bin/env bash
# =============================================================================
# cre_setup.sh - one-command preflight + bootstrap for the CRE collector.
#
# Run this FIRST on a fresh macOS or Ubuntu/WSL2 clone. It verifies the
# toolchain, installs Node deps, checks API readiness and the database env,
# and runs an optional network smoke test
# of the collect -> ingest plumbing. It never writes to the live database and
# never prints secrets.
#
# Usage:
#   bash cre_setup.sh              # full: checks + npm install + smoke test
#   bash cre_setup.sh --check      # read-only doctor (no install, no smoke)
#   bash cre_setup.sh --no-smoke   # checks + install, skip the network smoke
#   bash cre_setup.sh --no-install # checks (+ smoke if deps present), skip install
#   bash cre_setup.sh --reinstall  # force npm install even if node_modules exists
#
# Smoke source override:  CRE_SMOKE_SOURCE=interra-realty (default)
# API URL override:       FIRECRAWL_API_URL (preferred), or API_URL
# Env-file override:      CRE_ENV_FILE=/path/to/.env.local (else ~/Documents defaults)
#
# Exit code: nonzero only if a HARD prerequisite fails (tooling, deps, code
# health). Deploy-time gaps (stack down, env missing, TCC, smoke) are warnings.
# =============================================================================
set -uo pipefail   # deliberately NOT -e: run every check and tally results.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
FC_DIR="${FC_DIR:-$(cd "$DIR/../../.." && pwd)}"
SMOKE_SOURCE="${CRE_SMOKE_SOURCE:-interra-realty}"
API_URL="${FIRECRAWL_API_URL:-${API_URL:-http://localhost:3002}}"
API_URL="${API_URL%/}"
export FIRECRAWL_API_URL="$API_URL"
# Doctor imports must not create __pycache__ in the checkout.
export PYTHONDONTWRITEBYTECODE=1

DO_INSTALL=1; DO_SMOKE=1; FORCE_INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --check)     DO_INSTALL=0; DO_SMOKE=0 ;;
    --no-smoke)  DO_SMOKE=0 ;;
    --no-install) DO_INSTALL=0 ;;
    --reinstall) FORCE_INSTALL=1 ;;
    -h|--help)   sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

python3 "$DIR/cre_platform.py" || exit $?

PASS=0; WARN=0; FAIL=0
declare -a TODOS=()
ok()      { printf '  OK   %s\n' "$1"; PASS=$((PASS+1)); }
warn()    { printf '  WARN %s\n' "$1"; WARN=$((WARN+1)); }
fail()    { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL+1)); }
note()    { printf '       %s\n' "$1"; }
section() { printf '\n== %s ==\n' "$1"; }
todo()    { TODOS+=("$1"); }

# ---------------------------------------------------------------------------
section "1. Toolchain"
# ---------------------------------------------------------------------------
for bin in node npm npx python3 git curl; do
  if command -v "$bin" >/dev/null 2>&1; then
    ok "$bin ($("$bin" --version 2>&1 | head -1))"
  else
    fail "$bin not found on PATH"
    [ "$bin" = "node" ] && todo "Install Node 24 for this execution environment (nodejs.org; brew install node on macOS)."
    [ "$bin" = "python3" ] && todo "Install Python 3 (apt install python3 python3-venv on Ubuntu; brew install python on macOS)."
  fi
done

# psql: the ingestor finds it via PSQL_BIN or the libpq kegs, so PATH is optional.
PSQL_FOUND=""
for cand in "${PSQL_BIN:-}" /opt/homebrew/opt/libpq/bin/psql /usr/local/opt/libpq/bin/psql /usr/bin/psql /usr/lib/postgresql/*/bin/psql; do
  [ -n "$cand" ] && [ -x "$cand" ] && PSQL_FOUND="$cand" && break
done
[ -z "$PSQL_FOUND" ] && command -v psql >/dev/null 2>&1 && PSQL_FOUND="$(command -v psql)"
if [ -n "$PSQL_FOUND" ]; then
  ok "psql ($PSQL_FOUND)"
else
  fail "psql not found (PostgreSQL client)"
  todo "Install a PostgreSQL client: apt install postgresql-client on Ubuntu, or brew install libpq on macOS; alternatively set PSQL_BIN."
fi

# ---------------------------------------------------------------------------
section "2. Container stack (Firecrawl)"
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  ok "docker present"
  ctx="$(docker context show 2>/dev/null || true)"
  case "$ctx" in
    orbstack|desktop-linux|default) ok "docker context = $ctx" ;;
    *) warn "docker context = '${ctx:-unknown}' (verify the intended local engine)" ;;
  esac
else
  fail "docker not found"
  todo "Start your local container engine: Docker Desktop on Windows/WSL2, or OrbStack on macOS."
fi

firecrawl_api_ready() {
  local response
  # Ignore ~/.curlrc so it cannot turn this doctor into a POST or add retries.
  response="$(curl --disable -fsS --request GET --connect-timeout 2 --max-time 5 "$API_URL/" 2>/dev/null)" || return 1
  printf '%s' "$response" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    raise SystemExit(1)
if not isinstance(payload, dict):
    raise SystemExit(1)
if payload.get("message") != "Firecrawl API":
    raise SystemExit(1)
if payload.get("documentation_url") != "https://docs.firecrawl.dev":
    raise SystemExit(1)
' >/dev/null 2>&1
}

API_READY=0
if firecrawl_api_ready; then
  API_READY=1
  ok "Firecrawl API ready (GET only; does not prove scrape/worker health)"
else
  warn "Firecrawl API readiness failed (unavailable or wrong service; full stack health unverified)"
  note "Start it: (cd \"$FC_DIR\" && docker compose up -d api) then re-run this check."
  todo "Bring up the Firecrawl core: cd \"$FC_DIR\" && docker compose up -d api"
fi

# ---------------------------------------------------------------------------
section "3. Node dependencies"
# ---------------------------------------------------------------------------
if [ -d node_modules ] && [ "$FORCE_INSTALL" -eq 0 ]; then
  ok "node_modules present (use --reinstall to refresh)"
elif [ "$DO_INSTALL" -eq 1 ] && command -v npm >/dev/null 2>&1; then
  if [ -f package-lock.json ]; then
    note "running: npm ci"
    if npm ci >/tmp/cre_setup_npm.log 2>&1; then ok "npm ci complete"; else fail "npm ci failed (see /tmp/cre_setup_npm.log)"; fi
  else
    note "running: npm install"
    if npm install >/tmp/cre_setup_npm.log 2>&1; then ok "npm install complete"; else fail "npm install failed (see /tmp/cre_setup_npm.log)"; fi
  fi
elif [ ! -d node_modules ]; then
  warn "node_modules missing and install skipped (--check/--no-install)"
  todo "Install deps: (cd \"$DIR\" && npm ci)"
fi

# ---------------------------------------------------------------------------
section "4. Code health"
# ---------------------------------------------------------------------------
if [ -d node_modules ] && command -v npm >/dev/null 2>&1; then
  # Keep only the final 16 KiB of diagnostics in memory; pipefail preserves
  # the check's status without creating or truncating a shared temporary file.
  if TYPECHECK_OUTPUT="$(npm run typecheck 2>&1 | tail -c 16384)"; then
    ok "TypeScript typecheck passes"
  else
    fail "TypeScript typecheck failed (last 16 KiB of diagnostics below)"
    note "$TYPECHECK_OUTPUT"
  fi
else
  warn "skipped typecheck (node_modules not installed)"
fi
if PYTHON_OUTPUT="$(python3 - <<'PY' 2>&1 | tail -c 16384
from pathlib import Path

for name in ("cre_ingest.py", "cre_monitor.py", "cre_gate.py"):
    compile(Path(name).read_bytes(), name, "exec")
PY
)"; then
  ok "Python modules compile (ingest, monitor, gate)"
else
  fail "Python syntax check failed (last 16 KiB of diagnostics below)"
  note "$PYTHON_OUTPUT"
fi
# pytest is dev/CI only: the pipeline never needs it, so this is a soft check.
if python3 -c 'import pytest' >/dev/null 2>&1; then
  ok "pytest available ($(python3 -m pytest --version 2>&1 | head -1))"
  note "Run tests only in a separate git clone --no-hardlinks (never a worktree or runtime checkout)."
else
  warn "pytest not installed (test suite cannot run; the pipeline itself does not need it)"
  todo "Optional tests: use a separate git clone --no-hardlinks and follow SETUP.md's Isolated verification recipe (collector/sidecar Node dependencies plus a Python venv); never run suites from this runtime checkout."
fi

# ---------------------------------------------------------------------------
section "5. Database env (POSTGRES_URL discovery; value never printed)"
# ---------------------------------------------------------------------------
ENV_STATUS="$(python3 - <<'PY' 2>/dev/null
import contextlib, io, sys
import cre_ingest
try:
    with contextlib.redirect_stderr(io.StringIO()):
        url, path = cre_ingest.load_db_url(None)
    # Print only the path (never the URL) so logs stay clean of secrets.
    print("OK\t" + path)
except SystemExit:
    print("MISSING")
except Exception as e:  # noqa: BLE001
    print("ERROR\t" + type(e).__name__)
PY
)"
case "$ENV_STATUS" in
  OK*)  ok "POSTGRES_URL found in $(printf '%s' "$ENV_STATUS" | cut -f2-)" ;;
  MISSING)
    warn "No POSTGRES_URL env file found (ingest will fail until this is set)"
    note "Set CRE_ENV_FILE=/path/to/EQUIRE/.env.local, or place it at a default ~/Documents path."
    todo "Provide the EQUIRE .env.local: export CRE_ENV_FILE=/path/to/.env.local (it holds POSTGRES_URL*)." ;;
  *)    warn "env discovery error: $ENV_STATUS" ;;
esac

# ---------------------------------------------------------------------------
section "6. Scheduling readiness"
# ---------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
case "$DIR" in
  "$HOME"/Documents/*)
    warn "Clone is under ~/Documents (macOS TCC): scheduled launchd runs exit 126"
    note "Recommended fix: move the repo outside ~/Documents (e.g. ~/code/firecrawl)."
    note "Alternative: grant Full Disk Access to /bin/bash (System Settings > Privacy)."
    todo "Resolve TCC before loading schedules: relocate the clone, or grant /bin/bash Full Disk Access." ;;
  *)
    ok "Clone is outside ~/Documents (no TCC blocker for launchd)" ;;
esac
if [ -f launchd/install_launchd.sh ]; then
  ok "launchd generator present (launchd/install_launchd.sh)"
  note "Render + install (gated, no auto-load): bash launchd/install_launchd.sh all"
else
  warn "launchd/install_launchd.sh missing"
fi
else
  note "Linux/WSL2: no launchd or TCC checks; use cre_status.sh --scheduler external."
  note "If canonical agent context is installed, load it with: source ~/.agentic-assets/env"
  note "No scheduler is installed by this setup; visible-surface approval and sole-writer cutover are separate."
fi
# The tier dispatcher uses the collector's stdlib Python SharedLock authority
# and recovery-sync protocol, so there is no external lock tool to install.
if [ -x cre_status.sh ] || [ -f cre_status.sh ]; then
  ok "run-health command present (cre_status.sh)"
  if [ "$(uname -s)" = "Darwin" ]; then
    note "Check scheduled-run health: bash cre_status.sh"
  else
    note "Check artifacts and API readiness: bash cre_status.sh --scheduler external (does not verify an external scheduler)."
  fi
else
  warn "cre_status.sh missing (no run-health heartbeat available)"
fi

# ---------------------------------------------------------------------------
section "7. Network smoke test (public brokerage collect -> dry-run ingest)"
# ---------------------------------------------------------------------------
if [ "$DO_SMOKE" -eq 0 ]; then
  note "skipped (--check / --no-smoke)"
elif [ ! -d node_modules ]; then
  warn "smoke skipped: node_modules not installed"
elif [ "$API_READY" -ne 1 ]; then
  warn "smoke skipped: Firecrawl API readiness failed"
else
  mkdir -p out
  SMOKE_DIR="$(mktemp -d out/setup-smoke.XXXXXX)" || { warn "unable to reserve private smoke directory"; exit 1; }
  SMOKE_OUT="$SMOKE_DIR/artifact.json"
  trap 'rm -f "$SMOKE_OUT"; rmdir "$SMOKE_DIR" 2>/dev/null || true' EXIT
  note "This contacts the public brokerage site; max-items limits output, not inventory fetches."
  if [ "$SMOKE_SOURCE" = "interra-realty" ]; then
    note "Default Interra transport: one inventory GET, at most 32 MiB / 60 seconds; no detail requests."
  fi
  note "collect: $SMOKE_SOURCE (sale, max 3 items) -> $SMOKE_OUT"
  if npx tsx collect.ts --source="$SMOKE_SOURCE" --transaction=sale --max-items=3 --out="$SMOKE_OUT" >/tmp/cre_setup_smoke.log 2>&1; then
    ok "collect.ts produced an artifact"
    if python3 cre_ingest.py --in "$SMOKE_OUT" --dry-run >>/tmp/cre_setup_smoke.log 2>&1; then
      ok "cre_ingest.py --dry-run parsed the artifact (no DB writes)"
    else
      warn "dry-run ingest failed (see /tmp/cre_setup_smoke.log)"
    fi
  else
    warn "collect smoke failed for '$SMOKE_SOURCE' (see /tmp/cre_setup_smoke.log)"
    note "Source layouts drift; try CRE_SMOKE_SOURCE=<other> bash cre_setup.sh"
  fi
  rm -f "$SMOKE_OUT"
  rmdir "$SMOKE_DIR" 2>/dev/null || true
  trap - EXIT
fi

# ---------------------------------------------------------------------------
section "Summary"
# ---------------------------------------------------------------------------
printf '  %d OK, %d WARN, %d FAIL\n' "$PASS" "$WARN" "$FAIL"
if [ "${#TODOS[@]}" -gt 0 ]; then
  printf '\n  Remaining steps:\n'
  for t in "${TODOS[@]}"; do printf '    - %s\n' "$t"; done
fi
printf '\n  Runbook: SETUP.md   Supervised refresh contract: START_HERE.md\n'

if [ "$FAIL" -gt 0 ]; then
  printf '\n  Hard prerequisites failed. Resolve the FAIL items above before running the pipeline.\n'
  exit 1
fi
exit 0
