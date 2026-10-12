# Windows Docker Desktop onboarding

This guide sets up this fork's five-service Firecrawl core on Windows. It does not change the MacBook's OrbStack workflow, model routing, OCR configuration, or CRE production runtime. Keep verified computer-specific paths, installed versions, and hardware facts in Context Engineering's gitignored `local/machine.md`.

## Prerequisites and host settings

- Docker Desktop for Windows, matching the CPU architecture. AMD64 supports both Intel and AMD x64 PCs; ARM64 is for Windows on ARM.
- WSL 2 enabled and current, with Docker Desktop's **Use the WSL 2 based engine** setting enabled and Linux containers selected. A separate Ubuntu installation is optional; PowerShell can call Docker directly.
- Git for Windows, including Git Bash, and Python 3.11 or newer. Node.js/npm is needed for the pinned CLI and MCP wrappers, but not to build the API inside Docker.
- Hardware virtualization enabled. If Docker already starts and `docker info` succeeds, do not change BIOS settings merely because a Windows inventory flag reports virtualization differently under an active hypervisor.

Docker's [WSL guidance](https://docs.docker.com/desktop/features/wsl/) and Microsoft's [WSL configuration reference](https://learn.microsoft.com/en-us/windows/wsl/wsl-config) explain the backend and resource limits. For a 24-logical-processor, approximately 128 GB RAM workstation, an initial ceiling of 8 processors, 32 GB RAM, and 2 GB swap leaves room for other applications. Smaller computers need smaller limits. These are starting limits, not a benchmarked throughput target or permanently reserved memory.

WSL resource limits belong in `%USERPROFILE%\.wslconfig`, not a tracked repository file. They apply to all WSL 2 distributions. Changing them requires stopping Docker, shutting down WSL, and restarting Docker; first check for other running WSL work. Leave automatic Docker startup off if the stack is only needed during interactive work. The shared Compose resource limits and bounded log rotation remain unchanged.

From PowerShell in the repository root:

```powershell
docker context show
docker compose version
docker info --format 'CPUs={{.NCPU}} MemoryBytes={{.MemTotal}} OSType={{.OSType}}'
wsl --version
```

Expect `desktop-linux` and `OSType=linux`. If necessary, select the existing `desktop-linux` context after starting Docker Desktop. Do not select the Mac's `orbstack` context on Windows.

## Keep the API local to this computer

The shared Compose API port is published on all host interfaces by default. For local development, create an untracked `docker-compose.override.yaml` with a loopback-only port. Compose 2.24.4 or newer supports `!override`, which replaces the original port list rather than adding a second binding:

```yaml
services:
  api:
    ports: !override
      - "127.0.0.1:3002:3002"
```

Add `/docker-compose.override.yaml` to this clone's `.git/info/exclude` so the host overlay stays local. If an overlay already exists, preserve its other settings and review the merged ports instead of overwriting it. The example assumes the default internal API port of 3002. Match any deliberately customized internal port.

```powershell
docker compose config --quiet
git check-ignore docker-compose.override.yaml
```

Do not print the full resolved Compose configuration into shared logs: it can include environment secrets. After startup, `docker compose ps` should show API host binding `127.0.0.1:3002`. The existing browser sidecar binding is also loopback-only, on port 3003. This local overlay does not change a Mac clone's network behavior.

## Build and start the core stack

For a fresh clone, the root `.env` is optional for non-AI calls. If it already exists, preserve its values and do not copy `apps/api/.env.example` over it. Model/provider/OCR changes retain the existing guarded human-owned handoff in the [local development guide](../../../LOCAL_DEVELOPMENT_GUIDE.md#2-env-files).

```powershell
docker compose build api playwright-service nuq-postgres
docker compose up -d api
docker compose ps
Invoke-RestMethod http://localhost:3002/
```

Selecting `api` starts its dependencies: `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres`. PostgreSQL and RabbitMQ must become healthy before API startup. FoundationDB is an optional backend and is unnecessary for this PostgreSQL baseline; plain `docker compose up -d` would start it too.

The image builds provide the API's Node/pnpm, Go, Rust, and browser dependencies. Do not install those language toolchains or a host PostgreSQL server solely to run the containers. Source development outside Docker has additional requirements.

## Verify useful work

Use the real Git Bash executable for shell helpers from PowerShell. With Git's standard installation path:

```powershell
$env:PATH = 'C:/Program Files/Git/bin;' + $env:PATH
& 'C:/Program Files/Git/bin/bash.exe' -lc 'command -v python3; python3 --version; command -v docker'
& 'C:/Program Files/Git/bin/bash.exe' scripts/firecrawl-ops/firecrawl_healthcheck.sh --evidence-dir .scratch/windows-onboarding/health
python scripts/firecrawl-ops/local_api_smoke_matrix.py --out-dir .scratch/windows-onboarding/api-smoke --timeout 120 --poll-timeout 90
python scripts/firecrawl-ops/pdf_parse_canary.py --modes fast,auto --max-pages 2 --out-dir .scratch/windows-onboarding/pdf
python scripts/firecrawl-ops/firecrawl_compatibility_doctor.py
& 'C:/Program Files/Git/bin/bash.exe' scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown --json
python scripts/firecrawl-ops/local_agent_preflight.py --smoke-dir .scratch/windows-onboarding/api-smoke --require base_http
```

Adjust the executable path if Git is installed elsewhere. The session-only PATH prepend makes Git Bash available to child processes. `bash` on PATH can otherwise resolve to WSL's launcher, so do not assume it is Git Bash. Shell helpers require a working `python3` command inside Git Bash; the direct Python helpers can use the verified `python` executable from PowerShell.

The API smoke matrix checks root, scrape, map, search, fast PDF parsing, a single-URL batch, a one-page crawl, queue status, and active crawl visibility. Keep optional mutating browser/agent/support probes disabled. Search depends on external discovery services; distinguish an upstream search failure from a local Docker failure. The PDF fixture is checked into the repository.

The read-only preflight deliberately keeps async jobs degraded with `smoke_producer_contract_untrusted`, even after a fresh passing smoke file. It also distinguishes declared CLI/MCP pins from verified package execution. Keep those fail-closed classifications intact and record the separate live integration results; do not require those capabilities to report ready as an onboarding shortcut.

Also verify a scrape through the Playwright sidecar explicitly, including its health response and release of the page permit afterward. An ordinary scrape may use the fetch engine, so its success alone does not prove browser execution. Use `apps/playwright-service-ts/README.md` for the sidecar request contract.

The static compatibility doctor checks the exact CLI/MCP declarations without resolving packages or calling the host. Its optional `--run` MCP probe uses POSIX pipe polling and does not work with native Windows pipes; do not report it as passed on Windows. Verify MCP initialization and tool listing separately with a Windows-compatible stdio client and bounded timeouts. The wrapper pins remain authoritative; do not replace them with `latest`. Smoke artifacts belong under ignored `.scratch/`; record command results in the issue or PR rather than committing raw captures.

## Install local agent skills

The Mac installer `sync_agent_skills.sh` retains its Unix symlink/copy workflow. Do not assume it works unchanged on Windows. A local Windows installation can copy each canonical `SKILL.md` into `~/.agents/skills/firecrawl-ops` and `~/.agents/skills/firecrawl-local-api`, then create directory junctions named `scripts` and `references` pointing to this clone's `scripts/firecrawl-ops` and `docs/firecrawl-ops/references`. Client skill directories can use junctions to those installed skill folders.

Inspect existing destinations before creating junctions; preserve unrelated installed skills. This approach needs the clone to stay at its verified path. Refresh the standalone installed `SKILL.md` copies after changing the canonical skills. The shared repository's symlink entries should remain intact for Mac checkouts.

## Daily use and limitations

```powershell
docker compose up -d api
docker compose ps
docker compose logs api --tail 100
docker compose logs playwright-service --tail 100
docker compose stop
```

Stopping services preserves their containers. The root stack has no durable data volumes for PostgreSQL, Redis, or RabbitMQ; recreating/removing those containers is not a persistence strategy. This is a local development stack, not proof of production readiness. Avoid exposing the unauthenticated API outside loopback.

Non-AI scrape, map, crawl, batch, search, and basic PDF parsing are the onboarding scope. Summary/JSON/extract need a configured provider; advanced browser routes need Hangar, and layout-aware OCR needs its separately governed adapter. Do not change those configurations to make a basic smoke test pass.

CRE collector offline suites must run in a separate `git clone --no-hardlinks`, never a worktree that can resolve the live `.cre.lock`. When Windows-specific toolchains or shell assumptions prevent native tests, run the offline suites inside an isolated Linux Docker container against that separate clone and report that execution platform explicitly. Passing Linux tests does not prove native Windows support. The supervised CRE refresh host guard currently requires Darwin/macOS, so neither Linux containers nor WSL make that production runner portable. Do not bypass the guard, enable a scheduler, or perform CRE database writes during onboarding.

For a bounded no-write collector probe, inspect the source adapter before choosing it. `--max-items` limits emitted listings, not necessarily inventory network requests: SVN/Buildout still enumerates the complete inventory first. Interra's authoritative inventory adapter performs one size/time-bounded request and can project a three-row sample for `cre_ingest.py --dry-run`. Keep sample artifacts private and do not confuse a truncated sample with full-source refresh acceptance.
