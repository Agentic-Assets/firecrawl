#!/usr/bin/env bash
# =============================================================================
# cre_setup.sh - one-command preflight + bootstrap for the CRE collector.
#
# Run this FIRST on any fresh clone (Mac mini production, or this MacBook Pro
# for testing). It verifies the toolchain, installs Node deps, checks the
# Firecrawl stack and the database env, and runs an optional offline smoke test
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
# Smoke source override:  CRE_SMOKE_SOURCE=avison-young (default)
# Env-file override:      CRE_ENV_FILE=/path/to/.env.local (else ~/Documents defaults)
#
# Exit code: nonzero only if a HARD prerequisite fails (tooling, deps, code
# health). Deploy-time gaps (stack down, env missing, TCC, smoke) are warnings.
# =============================================================================
set -uo pipefail   # deliberately NOT -e: run every check and tally results.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
FC_DIR="${FC_DIR:-$(cd "$DIR/../../.." && pwd)}"
SMOKE_SOURCE="${CRE_SMOKE_SOURCE:-avison-young}"

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
for bin in node npm npx python3 git; do
  if command -v "$bin" >/dev/null 2>&1; then
    ok "$bin ($("$bin" --version 2>&1 | head -1))"
  else
    fail "$bin not found on PATH"
    [ "$bin" = "node" ] && todo "Install Node (brew install node), then re-run."
    [ "$bin" = "python3" ] && todo "Install Python 3 (preinstalled on macOS, or brew install python)."
  fi
done

# psql: the ingestor finds it via PSQL_BIN or the libpq kegs, so PATH is optional.
PSQL_FOUND=""
for cand in "${PSQL_BIN:-}" /opt/homebrew/opt/libpq/bin/psql /usr/local/opt/libpq/bin/psql; do
  [ -n "$cand" ] && [ -x "$cand" ] && PSQL_FOUND="$cand" && break
done
[ -z "$PSQL_FOUND" ] && command -v psql >/dev/null 2>&1 && PSQL_FOUND="$(command -v psql)"
if [ -n "$PSQL_FOUND" ]; then
  ok "psql ($PSQL_FOUND)"
else
  fail "psql not found (Homebrew libpq)"
  todo "Install libpq: brew install libpq  (the ingestor auto-detects /opt/homebrew/opt/libpq/bin/psql)."
fi

# ---------------------------------------------------------------------------
section "2. Container stack (Firecrawl)"
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  ok "docker present"
  ctx="$(docker context show 2>/dev/null || true)"
  if [ "$ctx" = "orbstack" ]; then
    ok "docker context = orbstack"
  else
    warn "docker context = '${ctx:-unknown}' (expected orbstack on these Macs)"
    note "Open OrbStack, or: docker context use orbstack"
  fi
else
  fail "docker not found"
  todo "Install OrbStack (https://orbstack.dev) and start it."
fi

if bash "$FC_DIR/scripts/firecrawl-ops/firecrawl_healthcheck.sh" >/dev/null 2>&1; then
  ok "Firecrawl API healthy at the configured URL"
else
  warn "Firecrawl stack not healthy (collect/monitor cannot run until it is up)"
  note "Start it: (cd \"$FC_DIR\" && docker compose up -d) then re-run this check."
  todo "Bring up the Firecrawl stack: cd \"$FC_DIR\" && docker compose up -d"
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
  if npm run typecheck >/tmp/cre_setup_tsc.log 2>&1; then ok "TypeScript typecheck passes"; else fail "typecheck failed (see /tmp/cre_setup_tsc.log)"; fi
else
  warn "skipped typecheck (node_modules not installed)"
fi
if python3 -m py_compile cre_ingest.py cre_monitor.py cre_gate.py 2>/tmp/cre_setup_py.log; then
  ok "Python modules compile (ingest, monitor, gate)"
else
  fail "py_compile failed (see /tmp/cre_setup_py.log)"
fi
# pytest is dev/CI only: the pipeline never needs it, so this is a soft check.
if python3 -c 'import pytest' >/dev/null 2>&1; then
  ok "pytest available ($(python3 -m pytest --version 2>&1 | head -1))"
  note "Run the suite any time: python3 -m pytest tests/ -q"
else
  warn "pytest not installed (test suite cannot run; the pipeline itself does not need it)"
  todo "Optional, dev/CI only: python3 -m pip install pytest  (then python3 -m pytest tests/ -q)."
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
section "6. Scheduling readiness (launchd)"
# ---------------------------------------------------------------------------
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
# The tier dispatcher uses a portable mkdir lock (no flock needed; stock macOS
# ships none), so there is no extra lock tool to install.
if [ -x cre_status.sh ] || [ -f cre_status.sh ]; then
  ok "run-health command present (cre_status.sh)"
  note "Check scheduled-run health any time: bash cre_status.sh"
else
  warn "cre_status.sh missing (no run-health heartbeat available)"
fi

# ---------------------------------------------------------------------------
section "7. Smoke test (offline collect -> dry-run ingest)"
# ---------------------------------------------------------------------------
if [ "$DO_SMOKE" -eq 0 ]; then
  note "skipped (--check / --no-smoke)"
elif [ ! -d node_modules ]; then
  warn "smoke skipped: node_modules not installed"
elif ! bash "$FC_DIR/scripts/firecrawl-ops/firecrawl_healthcheck.sh" >/dev/null 2>&1; then
  warn "smoke skipped: Firecrawl stack not healthy"
else
  SMOKE_OUT="out/setup_smoke.json"
  mkdir -p out
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
fi

# ---------------------------------------------------------------------------
section "Summary"
# ---------------------------------------------------------------------------
printf '  %d OK, %d WARN, %d FAIL\n' "$PASS" "$WARN" "$FAIL"
if [ "${#TODOS[@]}" -gt 0 ]; then
  printf '\n  Remaining steps:\n'
  for t in "${TODOS[@]}"; do printf '    - %s\n' "$t"; done
fi
printf '\n  Runbook: SETUP.md   Daily command: bash cre_daily_update.sh --no-mark-missing\n'

if [ "$FAIL" -gt 0 ]; then
  printf '\n  Hard prerequisites failed. Resolve the FAIL items above before running the pipeline.\n'
  exit 1
fi
exit 0
