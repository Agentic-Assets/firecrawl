#!/usr/bin/env bash
# Generic wrapper for running the C10 host coordinator inside a Linux
# container from a non-Linux operator machine (e.g. macOS).
#
# capacity_c10.host_store.PrivateReceiptStore is Linux-only by design and
# that check must never be weakened or bypassed. This script does not touch
# it, or anything else C10/JLL-specific: it only manages the generic runner
# container defined in docker-compose.c10-runner.yaml (build/up/exec/shell/
# down). What actually runs inside the container -- a preflight check, a
# pytest invocation, or (in a later, separately authorized phase) the real
# `python3 -m capacity_c10...` admission command -- is entirely up to the
# caller of `exec`.
#
# The runner reaches the *host* Docker daemon over a bind-mounted socket
# (Docker-outside-of-Docker) and shares that daemon's network namespace
# (`network_mode: host` in the compose file), so a C10 sidecar it starts via
# `docker compose ... playwright-service-c10` is a sibling container on the
# same daemon whose 127.0.0.1:<port> publication is directly reachable.
#
# Usage:
#   run_linux_controller.sh build            # build the runner image
#   run_linux_controller.sh up               # start the (idle) runner container
#   run_linux_controller.sh down             # stop and remove it
#   run_linux_controller.sh shell            # interactive shell inside it
#   run_linux_controller.sh exec CMD [ARGS...]   # run one command inside it
#   run_linux_controller.sh npm-install       # `npm install` for the collector,
#                                              # once, into the node_modules
#                                              # volume (needed before any tsx
#                                              # child can run)
#
# All subcommands are safe to run repeatedly; `up`/`exec`/`shell` start the
# container on demand if it is not already running. Nothing here builds or
# starts the C10 browser sidecar itself -- that is the coordinator's job, not
# this wrapper's.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.c10-runner.yaml"
SERVICE="c10-linux-runner"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "run_linux_controller.sh: missing ${COMPOSE_FILE}" >&2
  exit 1
fi

compose() {
  docker compose -f "${COMPOSE_FILE}" "$@"
}

usage() {
  sed -n '2,33p' "${BASH_SOURCE[0]}"
}

cmd="${1:-}"
if [[ -z "${cmd}" ]]; then
  usage
  exit 1
fi
shift || true

case "${cmd}" in
  build)
    compose build "${SERVICE}"
    ;;
  up)
    compose up -d "${SERVICE}"
    ;;
  down)
    compose down --remove-orphans
    ;;
  shell)
    compose up -d "${SERVICE}" >/dev/null
    compose exec "${SERVICE}" bash
    ;;
  exec)
    if [[ $# -eq 0 ]]; then
      echo "run_linux_controller.sh exec: missing command" >&2
      exit 1
    fi
    compose up -d "${SERVICE}" >/dev/null
    compose exec "${SERVICE}" "$@"
    ;;
  npm-install)
    compose up -d "${SERVICE}" >/dev/null
    compose exec "${SERVICE}" npm install \
      --prefix scripts/firecrawl-ops/cre_collector
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "run_linux_controller.sh: unknown subcommand '${cmd}'" >&2
    usage
    exit 1
    ;;
esac
