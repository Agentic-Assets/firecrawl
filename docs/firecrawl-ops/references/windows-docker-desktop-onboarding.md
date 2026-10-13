# Windows Docker Desktop onboarding

This guide sets up this fork's five-service Firecrawl core on Windows. It does not change the MacBook's OrbStack workflow, model routing, OCR configuration, or CRE production runtime. Keep verified computer-specific paths, installed versions, and hardware facts in Context Engineering's gitignored `local/machine.md`.

## Prerequisites and host settings

- Docker Desktop for Windows, matching the CPU architecture. AMD64 supports both Intel and AMD x64 PCs; ARM64 is for Windows on ARM.
- WSL 2 enabled and current, with Docker Desktop's **Use the WSL 2 based engine** setting enabled and Linux containers selected. A separate Ubuntu installation is optional; PowerShell can call Docker directly.
- Git for Windows, including Git Bash, and Python 3.11 or newer. Node.js/npm is needed for the pinned CLI and MCP wrappers, but not to build the API inside Docker.
- Hardware virtualization enabled. If Docker already starts and `docker info` succeeds, do not change BIOS settings merely because a Windows inventory flag reports virtualization differently under an active hypervisor.

Docker's [WSL guidance](https://docs.docker.com/desktop/features/wsl/) and Microsoft's [WSL configuration reference](https://learn.microsoft.com/en-us/windows/wsl/wsl-config) explain the backend and resource limits. For a 24-logical-processor, approximately 128 GB RAM workstation, an initial ceiling of 8 processors, 32 GB RAM, and 2 GB swap leaves room for other applications. Smaller computers need smaller limits. These are starting limits, not a benchmarked throughput target or permanently reserved memory.

WSL resource limits belong in `%USERPROFILE%\.wslconfig`, not a tracked repository file. They apply to all WSL 2 distributions. Changing them requires stopping Docker, shutting down WSL, and restarting Docker; first check for other running WSL work. Leave automatic Docker startup off if the stack is only needed during interactive work. The shared Compose resource limits and bounded log rotation remain unchanged.

With WSL, [Docker Resource Saver](https://docs.docker.com/desktop/use-desktop/resource-saver/)
does not stop the shared Linux VM or reclaim its memory. WSL integration can
also prevent entry into Resource Saver. Treat the ceilings above as the bound;
memory reclamation is a separate WSL setting, not a promised Resource Saver benefit.

From PowerShell in the repository root:

```powershell
docker context show
docker compose version
docker info --format 'CPUs={{.NCPU}} MemoryBytes={{.MemTotal}} OSType={{.OSType}}'
wsl --version
```

Expect `desktop-linux` and `OSType=linux`. If necessary, select the existing `desktop-linux` context after starting Docker Desktop. Do not select the Mac's `orbstack` context on Windows.
Inside an integrated Ubuntu distribution, the context can be `default` while
using the same Docker Desktop engine; verify `docker info` there rather than
creating a second daemon or forcing the Windows context name into Linux.

If Desktop is open but the engine is stopped, follow
[Windows Docker engine recovery](windows-docker-engine-recovery.md) before
changing WSL integration settings or reinstalling anything. Verify engine
readiness, Ubuntu integration and Firecrawl container health separately.

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

## Install repository and user agent skills

Repo skills live under `.agents/skills`; `.claude/skills` is a relative symlink to that directory. The two Docker skills (`multi-stage-dockerfile` and `docker-patterns`) include pinned source commits, hashes and original MIT licenses in their directories. They add guidance and do not install Docker services or change the runtime.

On native Windows, Git can materialize tracked symlinks as text placeholders. Enable Windows Developer Mode, or run this narrow repair from an elevated terminal:

```powershell
python scripts/firecrawl-ops/ensure_skill_links.py --apply
git config --local core.symlinks true
python scripts/firecrawl-ops/local_agent_preflight.py --skill-links-only
```

The helper restores only the five canonical relative symlinks, refuses unknown content and preserves Git's mode-120000 link blobs. No copy or junction fallback is silently substituted. On macOS/Linux, intact links are a no-op. For user-level installation on macOS/Linux, `sync_agent_skills.sh` uses rsync when available and a staged, rollback-safe `cp -RL` fallback otherwise. Existing user-level Windows junctions from a previous installation are separate from repository symlinks; preserve unrelated client configuration.

## Ubuntu workspace for the CRE runtime

The container API can run directly from PowerShell. The CRE runtime needs Linux/macOS POSIX locks and process groups. Install a user WSL distribution, then enable **Settings > Resources > WSL Integration > Ubuntu-24.04** in Docker Desktop. Verify Docker access inside that distro before starting another stack.

```powershell
wsl --install Ubuntu-24.04 --web-download --no-launch --version 2
wsl -d Ubuntu-24.04
```

Inside Ubuntu, use a regular Linux user and install `git ca-certificates curl jq rsync postgresql-client python3 python3-venv procps xz-utils` through apt. Install Node 24 for Linux from the official Node distribution with its published SHA-256 verified. Keep the clone under `~/code/firecrawl`, not `/mnt/c`, and run the collector's `npm ci`. Use a dedicated virtual environment for pytest; do not share Windows `node_modules` or install test packages into system Python. No PostgreSQL server or host Go/Rust/Chromium toolchain is required.

```bash
docker info
curl --fail http://localhost:3002/
cd ~/code/firecrawl/scripts/firecrawl-ops/cre_collector
bash cre_setup.sh --check
bash cre_status.sh --scheduler external
```

Missing production credentials and empty run history are honest status warnings. `external` mode retains lock and staleness checks and creates no scheduler. Provision secrets separately through the approved operator workflow. Use a separate `git clone --no-hardlinks` for the full offline suites.

### Minimal company context in Ubuntu

The verified minimal WSL context uses the existing canonical Windows Context
Engineering checkout through its Linux mount path, for example
`/mnt/c/Users/<WindowsUser>/GitHub/Agentic-Assets-Context-Engineering`. Resolve
the actual path with `wslpath`; keep the machine's exact path only in the
context root's ignored `local/machine.md`. `~/.agentic-assets/env` exports
`AA_CONTEXT_ROOT` and `AA_OPERATOR`; `~/.agentic-assets/operator` names the
operator (`cayman` on this workstation), and `~/.agentic-assets/machine` is
`client`. `~/AGENTS.md` is a symlink to the canonical root's `AGENTS.md`.

Verify those existing entries inside Ubuntu:

```bash
source ~/.agentic-assets/env
test -f "$AA_CONTEXT_ROOT/AGENTS.md"
test "$(cat ~/.agentic-assets/operator)" = "$AA_OPERATOR"
test "$(cat ~/.agentic-assets/machine)" = client
test "$(readlink -f ~/AGENTS.md)" = "$(realpath "$AA_CONTEXT_ROOT/AGENTS.md")"
```

This provisions context for the Linux workspace. It is not a completed
company `verify-install` run or proof that every global Linux agent client is
installed. It copies no Windows credentials and enables no scheduler.

### Supabase validation and database transport

An authenticated Supabase CLI is an alternative when the Codex Supabase connector needs reauthentication. Their login states are independent. Verify the current CLI help, signed-in identity and intended project before querying:

```powershell
supabase --version
supabase whoami --output-format json
supabase projects list --output-format json
supabase db query --help
```

CLI 2.120.0 supports `db query --linked --project-ref <verified-project-ref> --file <sql-file> --output-format json --workdir <ignored-scratch-directory>`. Both `--linked` and the explicit project ref are required for this Management API path. The scratch working directory contains CLI cache files; no persistent repository link or database password is needed for this path. Keep credentials and connection strings out of command logs. Wrap validation SQL in `BEGIN READ ONLY`, set a bounded local `statement_timeout`, and finish with `ROLLBACK`. Use the existing aggregate queries in `cre_validate.py`; do not infer provider freshness from ingestion timestamps. Report timed-out checks as unverified rather than raising limits until everything appears green.

Management API access does not prove that Ubuntu can open the collector's PostgreSQL connection. Check DNS, routing and the actual approved database transport separately. Supabase's [connection guide](https://supabase.com/docs/guides/database/connecting-to-postgres) explains that the default direct endpoint uses IPv6, while its shared session pooler supports IPv4. If the direct endpoint is unreachable from WSL, an operator-approved session-pooler endpoint or IPv4 add-on can provide an alternative. Preserve target-binding and credential checks; do not silently rewrite connection strings or change Windows network policy. Transaction pooling has different connection semantics and must be checked against the caller's contract before substitution. A CLI readback alone is not a full refresh or an authorization to change the live writer.

Use the [WSL Supabase connection troubleshooting guide](wsl-supabase-connection-troubleshooting.md)
for the verified IPv6 route failure, approved session pooler on5432, official
CA/verify-full repair, credential-free diagnostics and fresh checkpoint identity.
It separates a working database connection from production handoff acceptance.

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

CRE collector offline suites must run in a separate `git clone --no-hardlinks`, never a worktree that can resolve the live `.cre.lock`. Run the POSIX suites under Ubuntu/WSL or an isolated Linux Docker container against that separate clone and report the execution platform explicitly. Passing Linux tests does not prove native Windows support.

The supervised checkpoint runner selects Darwin Mach/sysctl telemetry on macOS
and Linux `/proc/stat`, memory PSI, available memory/swap and visible cgroup v2
limits/events on Linux. Unsupported platforms and missing safety evidence fail
closed. In WSL2/Docker Desktop these measurements cover the Linux VM and
visible cgroups, **not aggregate Windows host CPU or RAM**. Keep the WSL and
container resource ceilings and follow the exact Linux policy in
[`START_HERE.md`](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md#cpu-and-memory-safety-and-evidence).
Injected Darwin contracts remain tested; native Mac runtime verification was
not available during this Windows setup. Do not bypass guards, enable a
scheduler, cut over the production writer, or perform CRE database writes as
an onboarding shortcut. The Linux safety change merged after founder approval
in [PR108](https://github.com/Agentic-Assets/firecrawl/pull/108); production
cutover remains separately governed.

For a bounded no-write collector probe, inspect the source adapter before choosing it. `--max-items` limits emitted listings, not necessarily inventory network requests: SVN/Buildout still enumerates the complete inventory first. Interra's authoritative inventory adapter performs one size/time-bounded request and can project a three-row sample for `cre_ingest.py --dry-run`. Keep sample artifacts private and do not confuse a truncated sample with full-source refresh acceptance.
