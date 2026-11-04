# Windows Docker Desktop onboarding

This guide sets up this fork's five-service Firecrawl core on Windows. It does not change the MacBook's OrbStack workflow, model routing, OCR configuration, or CRE production runtime. Keep verified computer-specific paths, installed versions, and hardware facts in Context Engineering's gitignored `local/machine.md`.

## Backend choices

The verified baseline here uses WSL 2, Docker Desktop's default Windows
backend, with a separate Ubuntu ext4 workspace for the POSIX collector.
[Docker VMM](https://docs.docker.com/desktop/features/vmm/) is also available
on Windows starting with Desktop 4.86; Docker labels it Beta as of October 13,
2026.
Docker documents improved file I/O and idle-memory reclamation, but those are
not a measured CRE workload speedup. Keep the verified WSL baseline until a
separate bounded comparison proves resource, networking, file-sharing and
collector compatibility. Switching the container backend does not itself
remove the collector's Linux/macOS requirement.

The `desktop-linux` and `default` entries in **Settings > Builders** are
[BuildKit builders](https://docs.docker.com/build/builders/), not the VMM
selection or evidence of two independent VMs. Inspect the actual engine and
process/resource measurements before attributing load to a builder entry.

## Prerequisites and host settings

- Docker Desktop for Windows, matching the CPU architecture. AMD64 supports both Intel and AMD x64 PCs; ARM64 is for Windows on ARM.
- WSL 2 enabled and current, with Docker Desktop's **Use the WSL 2 based engine** setting enabled and Linux containers selected. A separate Ubuntu installation is optional; PowerShell can call Docker directly.
- Git for Windows, including Git Bash, and Python 3.11 or newer. Node.js/npm is needed for the pinned CLI and MCP wrappers, but not to build the API inside Docker.
- Hardware virtualization enabled. If Docker already starts and `docker info` succeeds, do not change BIOS settings merely because a Windows inventory flag reports virtualization differently under an active hypervisor.

Docker's [WSL guidance](https://docs.docker.com/desktop/features/wsl/) and Microsoft's [WSL configuration reference](https://learn.microsoft.com/en-us/windows/wsl/wsl-config) explain the backend and resource limits. For a 24-logical-processor, approximately 128 GB RAM workstation, an initial ceiling of 8 processors, 32 GB RAM, and 2 GB swap leaves room for other applications. Smaller computers need smaller limits. These are starting limits, not a benchmarked throughput target or permanently reserved memory.

For this desktop's ongoing CRE workload, the host ceiling was subsequently
raised to 12 processors, 48 GB RAM and 4 GB swap. The October 15 controlled stop/start
verified those effective VM limits and recovered the same five containers,
followed by a real non-AI scrape. API/browser container limits stayed unchanged.
This adds capacity headroom; it does not establish a throughput improvement or
prove recovery after a Windows reboot. Verify current limits rather than
treating a dated observation as live state.

WSL resource limits belong in `%USERPROFILE%\.wslconfig`, not a tracked repository
file. They apply to all WSL 2 distributions. Changing them requires stopping
Docker, shutting down WSL, and restarting Docker; first check for other running
WSL work.

For scheduled Windows CRE, turn on Docker Desktop's standard **Start Docker
Desktop when you sign in to your computer** preference. Leave automatic startup
off only when this stack is for interactive use. This starts Desktop in the
signed-in user's session; signed-out task execution is unsupported. The login
preference and container `unless-stopped` policies do not prove successful
login or reboot recovery; qualify those events separately under the
[CRE unattended acceptance guide](guarded-incremental-tiers.md#unattended-operating-goal).
See Docker's [Desktop settings reference](https://docs.docker.com/desktop/settings-and-maintenance/settings/).
The shared Compose resource limits and bounded log rotation remain unchanged.

Keep Linux pipeline code, `node_modules`, caches, output and Linux test clones
inside Ubuntu's filesystem, such as `/home/<user>/code/firecrawl`, rather than
running file-heavy Linux work against `/mnt/c/...`. Keep native PowerShell
editing and launchers in the Windows checkout. This follows Microsoft's
[filesystem performance guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems#file-storage-and-performance-across-file-systems).
Both locations are on this Windows PC. Deploy reviewed Git commits between the
checkouts; do not share `node_modules` or overwrite a running checkout with a
directory copy. A few explicit cross-filesystem handoff files do not require
moving the production runtime onto NTFS.

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

Container startup and API readiness are separate. The API currently has no Compose healthcheck, so even `up --wait` does not establish that its HTTP routes are ready. If the root check fails immediately after startup, allow a short initialization window and retry the healthcheck with a bounded timeout. If it remains unavailable, inspect API startup logs and loopback bindings before restarting containers or the engine; preserve existing configuration and runtime identities during diagnosis.

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

The [cross-platform credential layout](guarded-incremental-tiers.md#credential-layout-across-windows-and-mac)
distinguishes private CRE database/webhook files from Docker's root `.env`.

### Minimal company context in Ubuntu

The verified minimal WSL context uses the existing canonical Windows Context
Engineering checkout through its Linux mount path, for example
`/mnt/c/Users/<WindowsUser>/GitHub/agentic-assets/Agentic-Assets-Context-Engineering`. Resolve
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

CLI 2.120.0 supports `db query --linked --project-ref <verified-project-ref> --file <absolute-sql-file> --output-format json --workdir <absolute-ignored-scratch-directory>`. Resolve and verify both paths before invoking the CLI: relative SQL paths resolve against `--workdir`, so repeating that directory in both arguments can produce a missing-file error after network initialization. Both `--linked` and the explicit project ref select this Management API path; no persistent repository link or database password is needed.

This version's [linked-query preflight](https://github.com/supabase/cli/blob/753520fa7202ed5f8b25883a4c0aa7e5e9ad1fc4/apps/cli/src/commands/db/query/query.handler.ts#L250-L278) resolves a PostgreSQL connection before reading the file. Its [resolver](https://github.com/supabase/cli/blob/753520fa7202ed5f8b25883a4c0aa7e5e9ad1fc4/apps/cli/src/command-internal/db-config.layer.ts#L110-L190) can request a temporary login role with `read_only: false`, verify pooler connectivity, and remove network bans after repeated connection failures. `BEGIN READ ONLY` constrains the supplied validation transaction, not this earlier initialization. Use the existing approved `psql` transport or an authenticated HTTPS Management API query when the diagnostic must avoid those preflight operations. A file-read error alone does not prove that no external initialization occurred.

Keep credentials and connection strings out of command logs. Wrap validation SQL in `BEGIN READ ONLY`, set bounded local statement and lock timeouts, and finish with `ROLLBACK`. Use the existing aggregate queries in `cre_validate.py`; do not infer provider freshness from ingestion timestamps. Report timed-out checks as unverified rather than raising limits until everything appears green.

Management API access does not prove that Ubuntu can open the collector's PostgreSQL connection. Check DNS, routing and the actual approved database transport separately. Supabase's [connection guide](https://supabase.com/docs/guides/database/connecting-to-postgres) explains that the default direct endpoint uses IPv6, while its shared session pooler supports IPv4. If the direct endpoint is unreachable from WSL, an operator-approved session-pooler endpoint or IPv4 add-on can provide an alternative. Preserve target-binding and credential checks; do not silently rewrite connection strings or change Windows network policy. Transaction pooling has different connection semantics and must be checked against the caller's contract before substitution. A CLI readback alone is not a full refresh or an authorization to change the live writer.

Use the [WSL Supabase connection troubleshooting guide](wsl-supabase-connection-troubleshooting.md)
for the verified IPv6 route failure, approved session pooler on5432, official
CA/verify-full repair, credential-free diagnostics and fresh checkpoint identity.
It separates a working database connection from production handoff acceptance.

## Daily use and limitations

For supervised incremental CRE trials and whole-Windows load admission, use
the [guarded incremental tier guide](guarded-incremental-tiers.md). It explains
which commands are database-free, how the Linux recovery barrier behaves,
and the remaining scheduler acceptance steps. Do not enable legacy tier
commands as an unattended Windows shortcut.

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
Linux and native macOS offline regression proof is recorded separately on
[AGENTIC-3753](https://linear.app/agenticassets/issue/AGENTIC-3753); neither
substitutes for a production checkpoint or unattended acceptance. Do not
bypass guards, enable a scheduler, cut over the production writer, or perform CRE database writes as
an onboarding shortcut. The Linux safety change merged after founder approval
in [PR108](https://github.com/Agentic-Assets/firecrawl/pull/108); production
cutover remains separately governed.

For a bounded no-write collector probe, inspect the source adapter before choosing it. `--max-items` limits emitted listings, not necessarily inventory network requests: SVN/Buildout still enumerates the complete inventory first. Interra's authoritative inventory adapter performs one size/time-bounded request and can project a three-row sample for `cre_ingest.py --dry-run`. Keep sample artifacts private and do not confuse a truncated sample with full-source refresh acceptance.

## Foreground controlled full refresh from Windows

The Windows launcher also supports `-Tier series`, using the canonical
checkpoint-series coordinator. This is an additive database-writing refresh,
**not a dry-run**. It requires a reviewed full 40-character SHA, an explicit
`-WorkDeadlineSeconds` budget from 30 to 86400 seconds, and
`-AcknowledgeDatabaseWrites`. These flags do not grant production or scheduler
approval. Settle the previous sole writer and inspect its recovery/quality
evidence before a separately approved invocation. A full 51-source series may
exceed a one-day budget; the budget is not a completion guarantee.

From the native Windows clone, a telemetry-only check remains:

```powershell
.\scripts\firecrawl-ops\cre_collector\windows\Invoke-CreGuardedTier.ps1 -CheckOnly
```

`-CheckOnly` samples Windows only, writes no audit/log files, reads no notifier
secret and never starts WSL. It does not validate Linux readiness or approve
the following production command. After a reviewed deployment to the separate
ext4 Ubuntu runtime, replace the SHA placeholder and use an approved source key
(or `all`) and budget. For example, this is a one-hour **writing** invocation:

```powershell
$reviewedSha = '<approved deployed 40-character SHA>'
.\scripts\firecrawl-ops\cre_collector\windows\Invoke-CreGuardedTier.ps1 `
  -Tier series -Source hanley -ExpectedSha $reviewedSha `
  -WorkDeadlineSeconds 3600 -AcknowledgeDatabaseWrites
```

The foreground wrapper defaults shared acquisition `-Concurrency` to 1 and
accepts an explicit 1–6 for series only; adapters retain their own caps/pacing.
The default and explicit `1` retain JLL/JLL Investor detail concurrency 1;
explicit `2`–`6` select detail concurrency 2 for those adapters. This bounded
mapping retains rendered transport and wait times and enables a supervised C2
trial; it is not live capacity or throughput acceptance. JLL and JLL Investor
share one acquisition exclusion lane, so pipeline mode does not collect them
simultaneously. Brokerage slots and per-source detail requests are separate.
Cushman detail and NAI enumeration remain 1. The reviewed Colliers Coveo path
retains detail concurrency 1, a 3000 ms start interval and 60000 ms challenge
cooldown.
It pins page cap 400, three attempts per source and zero CPU recovery replays.
The coordinator inherits caller priority; canonical `--nice 15` adds 15 to
each checkpoint child's inherited nice value (15 from an ordinary nice-0
caller, capped by Linux at 19). It does not nest a second outer nice wrapper.
Linux CPU admission/guard settings are
90%, a 30-second sustain and a 2-second sample; the memory guard uses a
20-second sustain and a 2-second sample with the existing fail-closed thresholds.
It starts no Docker services and changes no configuration. Docker Desktop must
already be available in the selected signed-in user's session.

Series mode refuses `-Apply`, `-Batch` and `-DeadlineSeconds`; those retain
their existing monitor/enrich meanings. Conversely, monitor/enrich refuse the
series budget and write-acknowledgement flags. `-Source` selects one key or
`all`; `-Pipeline -Sources` accepts a finite comma-separated exact-key list
and conflicts with `-Source`. The Linux coordinator validates registry membership
before credentials or output allocation. No resume, retry-failed, lifecycle,
baseline or mark-missing controls are exposed by this Windows mode. Existing
Mac/manual invocation defaults are unchanged.

The SHA check binds clean deployed code and a matching local origin tracking
tip; it does not perform a remote fetch or prove remote freshness. Windows
admission must reach Linux within 15 seconds and applies only at startup.
Subsequent Linux CPU/RAM evidence covers the VM and visible cgroups, not
continuous whole-Windows load. The independent Linux work deadline requests
cooperative cleanup; cleanup may extend past the budget and remains unverified
when local settlement or receipt publication cannot be proved.

The existing bounded Windows stdout/stderr files and audit remain
informational. The coordinator's early stdout JSON contains the exact
`series_dir` and `invocation_id`. Copy those values for an operator stop; the
plain-directory capture hint does **not** parse that JSON or correlate an
invocation ID into the Windows audit. A successful final directory hint, native
exit, Windows audit or notification is not Linux settlement authority. Preserve
the exact early identity while it is available: each stream retains at most
1 MiB and later output may be discarded.

```powershell
.\scripts\firecrawl-ops\cre_collector\windows\Stop-CreCheckpointSeries.ps1 `
  -SeriesDirectory '/absolute/Linux/series-directory' `
  -InvocationId '<exact 32-character lowercase hex invocation ID>' -CheckOnly
# Remove -CheckOnly only for the exact operator-approved stop request.
```

Stop `-CheckOnly` prints arguments without WSL, owner validation or signaling.
The real wrapper forwards one request to the Linux pidfd-bound helper, preserves
its exit status, and uses a 60-second wait by default (allowed 1–120 seconds).
It does not apply a Windows CPU gate, select the latest run, kill WSL, force
child termination, clear a recovery fence or replay work. The thin stop wrapper accepts spaces and
apostrophes but refuses literal double quotes in Linux paths because Windows
PowerShell 5.1 can alter those characters during native argument forwarding;
use the Linux helper directly for such an unusual path. Linux validates the
private durable owner receipt; stop exit 0 means confirmed local settlement,
1 means refusal before a verified stop and 2 means timeout or an unverified
outcome. Local settlement does
not prove remote backend work or a database rollback. See the canonical
[series control contract](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md#optional-linux-work-deadline-and-exact-invocation-stop)
for terminal evidence and cleanup limits.

This foreground integration is engineering capability tracked by
[AGENTIC-4178](https://linear.app/agenticassets/issue/AGENTIC-4178).
It creates no task or recurring process. Supervised live acceptance, quality
decisions, notification destination and exact scheduling surface approval stay
under [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752).

## Recurring Windows execution

Routine acquisition does not need a continuously active AI agent. A
deterministic Windows Task Scheduler task can invoke a foreground WSL job,
retain the runner's locks/guards/logs, and expose status and failures. This is
the founder-approved desktop execution surface under
[AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752). The
[installation and acceptance procedure](guarded-incremental-tiers.md#windows-task-scheduler-installation-and-acceptance)
uses the repository installer, exact reviewed SHA, visible native task and
source-specific provider-to-database proof. Approval of the surface does not
mean every source or task is enabled; inspect the actual native task and the
company automation registry for current activation state. The alternative Codex
heartbeat remains paused.

Prefer inexpensive incremental work over frequent full-registry sweeps: the
broader future cadence is inventory checks twice daily, bounded detail
enrichment every four hours, and a weekly full guarded sweep. This proposal
does not describe the currently qualified task scope or enable unverified
tiers and sources. JLL, JLL
Investor, CBRE Deal Flow and Colliers are excluded from the cheap monitor;
they need separate admitted detail cadences. Do not describe the incremental
cadence as freshness for all 51 sources.

The default guarded monitor is a no-write trial. Its explicit apply mode can
feed eligible changes for existing listings into the detail queue after a
verified supervised pilot. It does not create un-ingested listings or queue
every kind of content change; the full sweep still supplies that acquisition.
See the [tier guide](guarded-incremental-tiers.md) for the exact write and queue
scope before choosing a recurring cadence.

Do not directly schedule the legacy monitor/enrich/weekly commands: their
equivalent resource/cancellation protection remains an acceptance gate in the
[guarded tier guide](guarded-incremental-tiers.md#unattended-operating-goal).
Use the proven guarded checkpoint path for a bounded pilot. A native task
needs tested graceful Linux cancellation, no overlap, bounded readiness,
reviewed deadlines and visible stop controls before activation. A Task
Scheduler hard stop is not proof that Linux children or backend work stopped.

Docker Desktop must be available in the task user's session. A locked screen
and a signed-out session are different. WSL systemd services do not keep a
stopped distro alive, so an internal timer is not an independent Windows wake
or login mechanism. Check Windows host load as well as VM telemetry when
admitting background work; Linux guards cannot observe unrelated Windows
applications. Recorded approval of the exact surface, its registry entry and
a tested bounded pilot are required before activating recurring production
execution. Whole-registry completion remains a separate acceptance item.

References: [Microsoft WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd),
[Docker Windows permissions](https://docs.docker.com/desktop/setup/install/windows-permission-requirements/),
and [Task Scheduler hard termination](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-allowhardterminate).

## Shared foreground pipeline setting

Windows/WSL uses the same collector pipeline as macOS. Opt in through the native
series wrapper with `-Pipeline -SourceWorkers 2 -Sources interra-realty,faris-lee`
and the matching explicit admissions, reviewed SHA and existing write/budget gates.
Change `-SourceWorkers` in the invocation/task preview to choose 1–4; omitted
pipeline mode resolves 2 and existing serial invocations remain 1. Acquisition
`-Concurrency` is separate from `-SourceWorkers`. The foreground launcher and
installer accept explicit series values 1–6; omitted installer actions retain
default 1. This does not change Docker limits, provider/detail limits, Mac
settings or scheduler cadence.
See [Start Here](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md#opt-in-stage-overlap)
for shared validation, serial ingestion and recovery requirements.
