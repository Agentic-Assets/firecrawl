Parent: [AGENTIC-3745](https://linear.app/agenticassets/issue/AGENTIC-3745)

# Recover a stopped Windows Docker engine

Docker Desktop's window can be open while its Linux engine is stopped. Check
the engine separately before diagnosing missing Ubuntu integration or changing
the repository. This procedure applies to Windows Docker Desktop; the Mac
continues to use its existing OrbStack workflow.

## Diagnose before changing settings

From PowerShell, inspect the installed commands and current state:

```powershell
docker desktop --help
docker desktop status
C:/Windows/System32/wsl.exe --version
wsl --list --verbose
```

If Desktop is stopped, use its supported start command. If its application is
already running but the engine remains stopped, inspect the fresh backend
failure before attempting a restart. Desktop's host logs normally live under
`%LOCALAPPDATA%/Docker/log/host/`; inspect them locally and redact secrets before
sharing. A missing Ubuntu Docker CLI/socket while `docker-desktop` is stopped
does not by itself prove that the integration setting was disabled.

On a Desktop version with the [Docker Desktop CLI](https://docs.docker.com/reference/cli/docker/desktop/), use its documented [bounded start command](https://docs.docker.com/reference/cli/docker/desktop/start/):

```powershell
docker desktop start --help
docker desktop start --timeout 90
# After reviewing the failure and checking that existing work can be interrupted:
docker desktop restart --help
docker desktop restart --timeout 90
```

A restart can interrupt containers and their clients. Check other users of
the engine first. It does not replace a model/OCR transition approval, and
must not be followed by an unrelated Compose rebuild or recreation.

## Verify the engine, integration and workload separately

```powershell
docker desktop status
docker context show
docker info --format 'Version={{.ServerVersion}} OSType={{.OSType}} CPUs={{.NCPU}}'
docker ps --all
wsl -d Ubuntu-24.04 -- docker info --format 'Version={{.ServerVersion}} OSType={{.OSType}} CPUs={{.NCPU}}'
```

Use the actual installed Ubuntu distribution name. Expect the Windows
`desktop-linux` context and Linux containers; Ubuntu may use `default` for the
same engine. An engine that responds is not proof that Firecrawl is healthy.
Inspect the existing five core containers, their limits and loopback bindings,
then follow the [onboarding verification](windows-docker-desktop-onboarding.md#verify-useful-work).
Prefer starting existing reviewed containers when appropriate; preserve their
configuration and local queue data. CRE writes still require the
[supervised checkpoint procedure](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md#strict-listing-refresh)
and current sole-writer coordination.

## Windows startup and container restart policy

For scheduled Windows CRE, turn on Docker Desktop's standard **Start Docker
Desktop when you sign in to your computer** preference in Settings > General for
the task user's interactive sign-in. Leave it off only when this stack is for
interactive use. CRE's task uses that signed-in user's token; signed-out
execution is unsupported. Verify the login preference separately from the
containers' restart policies: a running Desktop window, a login entry and a
container policy each prove different settings. See Docker's [Desktop settings
reference](https://docs.docker.com/desktop/settings-and-maintenance/settings/).
Inspect only the intended five core containers; do not change every container
on the computer. Docker supports updating an existing container's policy with
`docker update --restart unless-stopped <verified-container-id>` without
recreating it. Reinspect its identity, image, resource limits and policy.

Keep Windows-only restart settings in a private Compose override, used alongside
the reviewed root Compose file for future recreation. Do not put the host's
startup preference into shared Compose or the Mac configuration. Record the
override's path in the host's ignored `local/machine.md`.

`unless-stopped` respects a manual stop, including across an engine restart;
it does not make a deliberately stopped stack ready for collection. The
foreground Windows launcher waits briefly for the local API and then refuses
dispatch if unavailable. It never restarts Docker. Task scheduling requires
separate [native run acceptance](guarded-incremental-tiers.md#windows-task-scheduler-installation-and-acceptance).
Verified startup settings and a manual scrape do not prove reboot/login
recovery; record that test separately when actually observed.

See Docker's [restart policy documentation](https://docs.docker.com/engine/containers/start-containers-automatically/).

## Distinguish WSL observation timeouts from workload failure

`Wsl/Service/WSAETIMEDOUT` on a new CLI connection does not establish that an
existing Linux workload or Docker engine died. Microsoft defines Winsock
[`WSAETIMEDOUT` (10060)](https://learn.microsoft.com/en-us/windows/win32/winsock/windows-sockets-error-codes-2)
as a connection timeout; the code alone does not identify the underlying cause.
Preserve the original running command handle and the failed observation's
arguments, timestamps, stderr and native exit code. Let that bounded observation
settle before considering another observation; do not accumulate hung probes.

Do not restart a source, Docker or WSL, shut down distributions, or reset the
host solely because an observation timed out. Require advancing evidence from
the exact run log and current guards, alongside the existing engine/container
identity and state. A live PID or a distribution labeled `Running` alone does
not demonstrate useful progress or health.

Native PowerShell read-only access to an exact known log through
`\\wsl.localhost\<distribution>\...` may still work when new CLI connections fail.
It is not guaranteed. Keep reads bounded and preserve their own failures; never
execute, edit or copy runtime code through that UNC path. File observations do
not bypass canonical write, AFTER-capture, ownership or settlement gates.

Capture `$LASTEXITCODE` immediately after the native command, before later
PowerShell commands can obscure its result. On October 17, an outer observation
handle (`88216`) returned zero despite the contained WSL failure; that outer
result did not prove the Linux observation succeeded.

In the October 17, 2026 observation on `85110e`, new CLI connections timed out
twice at 12:56–12:58 UTC while original source handle `31597` remained live.
The exact saved run log advanced from 1,500 to 1,700 sale details by 12:59:06;
API/browser start identities were unchanged, restart counts were zero and OOM
flags false. Existing 12:59:53 Linux evidence recorded normal memory and 13.9%
CPU. These observations establish continued work, not a root cause, CLI recovery
or a guaranteed UNC workaround. Consult [Microsoft's WSL troubleshooting guide](https://learn.microsoft.com/en-us/windows/wsl/troubleshooting-guide)
before proposing a separately reviewed repair.

## Observed recovery and limits

On October 13, 2026, Docker Desktop 4.95.0 reported a stopped Linux engine.
Its backend log recorded a failed precondition while running
`C:/Windows/System32/wsl.exe --version`, with `The file cannot be accessed by
the system.` Ubuntu could still execute a PostgreSQL read-only canary, while
Docker's Linux CLI mount and socket were absent.

The same native WSL command subsequently succeeded and reported WSL 3.0.3
with kernel 6.18.54.1, newer than the previous setup snapshot. A supported
Desktop restart restored engine 29.8.2 with the configured eight CPUs, and
Ubuntu could query that engine again. No WSL reinstall, distribution removal,
network-policy change or Docker data reset was needed. The version change and
recovery are verified observations; they do not establish why the earlier
launcher call failed. Engine recovery does not establish container health or
production-refresh completion.

Across October 18–19, three scheduled Windows attempts exhausted the launcher's
90-second loopback API readiness wait and refused before Linux dispatch or
database work. Docker Desktop and its backend were absent at diagnosis. The
operator used the supported bounded `docker desktop start --timeout 60`; the
five existing Firecrawl containers returned with their reviewed configurations
and the API became ready. The standard sign-in startup preference was enabled.
Subsequent on-demand Interra (59 listings) and Newmark (4,548) runs passed source readback,
no-regression checks, local settlement and actual Slack readback. These manual
recoveries do not prove natural-clock or login/reboot acceptance. Why Desktop
had stopped remains unknown. No Mac, resource-limit or model-routing changes
were made. [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752)
holds the dated evidence.

If this recurs, record the fresh failure timestamp and executable arguments,
compare the exact WSL command under the same account/session as Desktop, and
inspect package/update events before choosing a repair. Check other WSL work
before any `wsl --shutdown`, which stops all distributions. Avoid speculative
kernel changes, permission rewrites, factory reset or unregistering a distro.

References: [Docker troubleshooting](https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/),
[Microsoft WSL troubleshooting](https://learn.microsoft.com/en-us/windows/wsl/troubleshooting),
and [WSL 3.0.3 release notes](https://github.com/microsoft/WSL/releases/tag/3.0.3).
