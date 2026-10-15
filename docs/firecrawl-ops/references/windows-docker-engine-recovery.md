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

On a Desktop version with the CLI shown above:

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

For an approved Windows unattended workload, verify Docker Desktop's login
startup separately from the containers' restart policies. A running Desktop
window, a login entry and a container policy each prove different settings.
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

If this recurs, record the fresh failure timestamp and executable arguments,
compare the exact WSL command under the same account/session as Desktop, and
inspect package/update events before choosing a repair. Check other WSL work
before any `wsl --shutdown`, which stops all distributions. Avoid speculative
kernel changes, permission rewrites, factory reset or unregistering a distro.

References: [Docker troubleshooting](https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/),
[Microsoft WSL troubleshooting](https://learn.microsoft.com/en-us/windows/wsl/troubleshooting),
and [WSL 3.0.3 release notes](https://github.com/microsoft/WSL/releases/tag/3.0.3).
