# Generic Linux runner for the C10 host coordinator.
#
# This image exists for exactly one reason: capacity_c10.host_store.PrivateReceiptStore
# is intentionally Linux-only (it refuses on any other sys.platform, and that
# refusal must never be weakened or bypassed). The C10 Python controller and its
# typed TypeScript child (`node --import tsx`) must therefore run on Linux even
# when the operator's own machine is macOS. This image is a plain, general
# "python3 + node + docker CLI" runner — it has no JLL-specific or C10-specific
# logic baked in. What it runs is chosen entirely by the caller (see
# tools/run_linux_controller.sh).
#
# It talks to the *host's* Docker daemon over a bind-mounted socket
# (Docker-outside-of-Docker) rather than running its own nested daemon, so the
# C10 sidecar it starts via `docker compose` is a sibling container on the same
# daemon, not a container-in-a-container.
#
# NOT SAFE FOR ANY LOCK-HOLDING ACTION ON MACOS/ORBSTACK HOSTS: `fcntl.flock`
# does not coordinate between a macOS host process and a process in this
# container reached over a bind-mounted repo. Never acquire the canonical CRE
# lock from a process running in this image; the caller (see
# docker-compose.c10-runner.yaml) sets CRE_LOCK_DOMAIN_UNTRUSTED so
# SharedLock.acquire and c10_linux_preflight.py fail closed instead.
# node:22 matches the collector's own package.json engine requirements
# (e.g. `firecrawl`/`@mendable/firecrawl-js` require node >=22); a lower
# major produced EBADENGINE warnings during `npm install` here.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        docker.io \
        git \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Debian bookworm's `docker.io` package does not ship the `compose` CLI
# plugin (no `docker-compose-v2`/`docker-compose-plugin` package in the
# default repos), and host_sidecar.py invokes `docker compose` (v2 subcommand
# form), not the standalone v1 `docker-compose` binary. Install the official
# static plugin binary directly instead of adding Docker's apt repo just for
# one binary.
ARG DOCKER_COMPOSE_PLUGIN_VERSION=2.29.7
RUN arch="$(dpkg --print-architecture)" \
    && case "${arch}" in \
         amd64) compose_arch="x86_64" ;; \
         arm64) compose_arch="aarch64" ;; \
         *) echo "unsupported arch: ${arch}" >&2; exit 1 ;; \
       esac \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL \
        "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_PLUGIN_VERSION}/docker-compose-linux-${compose_arch}" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && /usr/local/lib/docker/cli-plugins/docker-compose version

WORKDIR /workspace/firecrawl

# No ENTRYPOINT/CMD: the wrapper script decides what to run each time
# (a preflight check, a shell, or a one-off python -m invocation), and always
# passes an explicit command rather than relying on an image default.
