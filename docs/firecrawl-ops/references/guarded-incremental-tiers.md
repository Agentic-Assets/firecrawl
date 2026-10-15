Governed by [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752), implementation [AGENTIC-4141](https://linear.app/agenticassets/issue/AGENTIC-4141) and [AGENTIC-4154](https://linear.app/agenticassets/issue/AGENTIC-4154).

# Guarded incremental CRE trials

The Windows desktop uses Docker Desktop with the existing WSL2 backend and an
ext4 Ubuntu checkout. `cre_guarded_tier.py` adds a foreground trial entrypoint
for explicit-source inventory observation and small enrichment batches. It
does not install or enable a scheduler. Existing Mac launchd commands retain
their behavior; they do not acquire these new protections automatically.

## What each mode does

| Invocation | Provider work | Database work |
| --- | --- | --- |
| `monitor --source KEY` | Current inventory for that source, collector concurrency1 | None; monitor transforms the artifact in dry-run mode |
| `monitor --source KEY --apply` | Same complete inventory check | Existing monitor audit job, index observations, events, neutral listing metadata and eligible enrichment queue entries |
| `enrich --source KEY` | None | None; prints the proposed queue claim SQL |
| `enrich --source KEY --apply` | Details for an exact-source queue batch | Claims, additive listing ingest, queue completion/retry accounting |

JLL, JLL Investor, CBRE Deal Flow and Colliers
SalesTracker are excluded from inventory monitoring; use separately admitted
checkpoint runs. The default inventory trial does not enqueue work. Neither
inventory mode refreshes listing detail. The enrichment SQL dry-run does not
test rendering.

Explicit monitor apply reuses the existing transactional worker. It updates
source-index observations/presence, records its audit job and events, and may
update existing listings' `source_lastmod` and `canonical_key`. Those neutral
updates can advance trigger-managed `updated_at`; that timestamp is not proof
of fresh listing detail. It does not change listing status or deletion.

Queueing has a narrower scope than general discovery: a first-ever source-index
seed emits no events or queue entries. A newly enumerated identity without an
existing listing row is counted as `enumerated_unmatched` and is not queued,
even on later monitor passes. For existing listings, observed status and price
fingerprint changes can queue enrichment; revision-only, text/image/document-only
changes and reappearance alone do not automatically queue it. Full acquisition
remains responsible for creating previously un-ingested listings.

Unbound Matthews enrichment is refused before listing ingestion or claim
completion. Its caller-bound targeted contract requires a current sitemap,
genuine detail observations and the existing Linux/WSL guarded owner, with a
work deadline of at most 600 seconds. The source-specific
[Matthews admission reference](matthews-enrichment-admission.md) records the
contract and its acceptance status. A source adapter registration or successful
partial detail fetch alone does not grant queue-write authority.

An applied enrichment uses the existing worker semantics, with an additional
database target fingerprint checked before claims and passed to the ingest
child. An uncertain bound-ingest failure retains claims for reconciliation.
Applied monitor work similarly binds the database target before loading prior
state or executing its transaction. Its summary is emitted before SQL execution:
enqueue counts are proposed candidates, not committed inserts or net queue
growth. Require terminal success and exact database readback for that proof.

No mode enables status activation, listing mark-missing, coverage-baseline updates, OM extraction
or DDL. No-write inventory success does not admit a source through the strict
coverage gate or certify a full refresh.

## Run a supervised trial

First read `cre_status.sh --scheduler external`, current sole-writer evidence,
the exact pushed commit and the source's current admission contract. Refresh
remote tracking refs explicitly before a run. The entrypoint requires a clean
checkout, exact expected SHA and a matching local `origin` tracking tip; it
does not fetch, pull or prove current remote state itself.

From PowerShell, sample Windows without starting WSL or writing files:

```powershell
pwsh -NoProfile -File scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1 -CheckOnly
```

Run a bounded no-write inventory trial after deploying the reviewed commit to
the existing Ubuntu checkout:

```powershell
pwsh -NoProfile -File scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1 `
  -Tier monitor -Source interra-realty -ExpectedSha <full-reviewed-sha> `
  -DeadlineSeconds 600
```

The defaults select `Ubuntu-24.04`, Linux user `cayman`,
`/home/cayman/code/firecrawl`, and the private
`/home/cayman/.config/cre/equire.env`. Override the explicit parameters on
another host. Never pass a database URL or password on the command line.
The Windows launcher explicitly pins Firecrawl to `http://127.0.0.1:3002`
inside Ubuntu; it does not inherit a hosted Firecrawl endpoint.

Linux operators can invoke the same foreground entrypoint directly:

```bash
python3 cre_guarded_tier.py monitor --source interra-realty \
  --expected-sha <full-reviewed-sha> --deadline-seconds 600
python3 cre_guarded_tier.py enrich --source hanley --batch 2 \
  --expected-sha <full-reviewed-sha> --deadline-seconds 600
```

Adding `--apply` to the enrichment command performs real queue and listing
writes. Adding it to monitor performs the observation, neutral metadata and
queue transaction described above. Require a permitted supervised trial, approved target,
healthy existing services and exact artifact/database readback before doing
that. Use the real generation artifact to assess data freshness and quality;
a successful worker exit alone is not a production acceptance certificate.
Do not run an applied trial while a full refresh owns the canonical lock.

## Protection and operator controls

The Linux owner arms the existing canonical recovery fence before any native
child, reuses checkpoint CPU/memory watchdogs and owns each subprocess group.
Enrichment claims are capped at50; monitor checks the complete selected-source
inventory and its transaction is not limited by that batch parameter. The work
deadline is30 to14400 seconds. Applied Matthews enrichment is limited to600
seconds and its one-shot Linux/WSL ownership contract; a longer generic deadline
is refused before claims. Cleanup
has the checkpoint's separate grace/join budgets; the work deadline does not
preempt a blocked kernel call or certify cancellation of remote work.

The PowerShell launcher measures whole-Windows CPU and physical memory using
Microsoft's native APIs. It refuses admission at90% CPU or below8GiB available
physical memory, and refuses missing, invalid, stale or unsupported-topology
evidence. These are conservative local admission settings, not Microsoft
standards. The Linux owner rejects stale/future Windows admission timestamps
before work. This is start-time host admission. Later native Windows load is
not monitored; Linux watchdogs observe the VM and visible cgroups. Retain the
current WSL ceiling and reassess from measured runs.

Linux receipts and guard journals are under `out/guarded-tiers/<run>/`.
Windows launch audit receipts are under `out/windows-launcher/` in the Windows
checkout. They are audit artifacts, not locks or scheduling authority. Receipt
paths and exit status are returned in the foreground.

### Windows output and audit

For Windows output capture, start the launcher in a fresh PowerShell process,
using `pwsh.exe -NoProfile -File <launcher-path> ...` or
`powershell.exe -NoProfile -File <launcher-path> ...`. Before admission sampling,
a normal invocation publishes an atomic `admission_pending` audit and prints
its audit, stdout and stderr paths. Read a running stream with
`Get-Content -LiteralPath <stdout-path> -Wait`; stderr has its own file. The
sidecars share the existing flat audit basename. `-CheckOnly` writes none of
these files. Parser and parameter-binding failures occur before the script body
and therefore cannot publish an audit.

Each stream stores at most1MiB of raw bytes, including UTF8 output. The helper
continues draining and discarding after that cap or a sink-write failure.
Counts, truncation, EOF and read/write/flush/close failures are recorded in the
audit, which has its own64KiB publication cap. `CapturedBytes` counts completed
writes; a partial failed write has uncertain retention. A capped file can end
within a UTF8 character. Intentional truncation
alone preserves the native exit code; an actual capture failure makes the
wrapper nonzero even when the native exit was0. Output is retained in files
rather than echoed without a bound into the foreground console.

The native process remains foreground for its whole lifetime. Only the drain
after native exit is time bounded: up to2 seconds for EOF, then up to1 second
per reader to join after cancelling that reader's pending Windows IO. A
descendant retaining a pipe can produce incomplete capture. An unjoined reader
or start-attempt/post-start helper uncertainty permanently refuses subsequent
captures in that loaded helper; use a fresh `-File` host. This does not kill processes,
terminate WSL, or establish Linux settlement. Once native exit is observed, a
reader that cannot join is reported as incomplete. A blocked sink can also
fill a pipe and prevent native exit; this wrapper has no outer hard deadline.
The Linux owner's independent work deadline remains a separate boundary.

Audit `dispatch=pending` means the helper may have attempted dispatch if it
subsequently threw or disappeared. `attempted` records a returned start-attempt
result; `returned` records an observed native exit. `nativeExitCode` and
`wrapperExitCode` are separate; existing `exitCode` remains the observed native
code when available. Terminal-audit failure returns1 and by default leaves the previous
valid receipt, which may still show pending work. Admission refusal keeps
ordinary launcher exit1; CheckOnly resource refusal uses75. These audit statuses
never certify a refresh or clear a Linux fence.

`captureHelperSha256` identifies the exact helper bytes decoded and compiled;
a mismatched cached helper type refuses dispatch. `launcherFileSha256Observed`
is an informational disk-file observation, not proof of the bytes previously
parsed by PowerShell. An exact early Linux run-directory line in stdout is
retained as `ObservedRunDirectory`, with conflicting references flagged. It is
only an operator correlation hint. The owner-bound stop helper still validates
the selected run independently, and `linuxSettlement` always remains
`unverified` in the Windows launcher audit. Logs retain child output locally;
the launcher does not dump environment values or exception details into audits.
Child stdout/stderr can contain business data or diagnostics. Keep these ignored
local artifacts private and review/redact them before sharing.

The capture helper leaves ordinary argument atoms unquoted so actual WSL
recognizes options such as `-d` and `--exec`; empty arguments, whitespace and
embedded quotes retain Windows argument escaping. The opt-in
[`Test-CreWslCapture.ps1`](../../../scripts/firecrawl-ops/cre_collector/windows/Test-CreWslCapture.ps1)
checks the shipped helper against the System32 WSL executable using only a
synthetic environment value and literal arguments. Run it in fresh Windows
PowerShell 5.1 and installed PowerShell 7 hosts with `-NoProfile -File` and
`-RunWslRoundTrip`. Without that switch it creates no files and dispatches
nothing. This proof establishes argument transport; it does not run a CRE tier
or establish provider, database or Linux-owner settlement.

### Optional Windows notifications

`-AlertWebhookFile <absolute-Windows-path>` optionally enables one failure-only
notification per launcher invocation by default (`-NotificationMode Failure`).
Provision the endpoint file outside every
Git checkout, under the scheduled Windows user's ownership. Its Windows ACL
must grant access only to that user, SYSTEM and Administrators. The helper
validates the actual opened handle before reading: a local fixed-drive regular
file, no reparse point or additional hardlinks, at most 4096 bytes, with the
required owner/ACL. Use UTF8 without a BOM, containing one well-formed HTTPS
URL and optionally one final newline. Broad ACLs, alternate streams, multiline
input, URL credentials/fragments and invalid endpoints refuse notification.
Do not put the endpoint itself in task arguments, environment values or Git.

For example, add only the file reference to the existing foreground command:

```powershell
-AlertWebhookFile 'C:\Users\<user>\.config\cre\failure-webhook.url'
```

The default is unconfigured and quiet. In `Failure` mode, successful work does not read the file
or send, and `-CheckOnly` never initializes notification, reads its secret or
sends, including when admission fails. Ordinary admission, native-start,
capture and initial/terminal audit failures can attempt a notification while
the script body still has control. The fixed payload contains the Windows
attempt ID, tier/source and observed exit codes, and explicitly says Linux
settlement is unverified. It includes no environment, argv, business output
or exception text. The endpoint and payload reach curl only through stdin;
curl output is discarded except for its three-byte HTTP status.

Curl has a 10-second request timeout, no retries, no redirects, no URL globbing,
verified TLS, and its default config file disabled. The local helper waits up
to 11 seconds for native exit plus up to 1.5 seconds of IO join/cancellation;
filesystem/setup or kernel stalls are not a hard total-duration guarantee.
An unexited curl or unjoined IO is `unverified`, retains unknown evidence as
applicable and refuses further sends in that loaded helper; use a fresh
`-File` shell. Start-attempt or IO-setup uncertainty uses the same conservative
status and fresh-host requirement. No process is killed or claimed settled by
a timeout.

To request a concise start and terminal success/failure pair, add
`-NotificationMode StartFinish` alongside the private file reference. The
scheduled-task installer accepts and forwards both options; its preview/action
contains only the file path. `StartFinish` requires a valid private endpoint
file, checked before actual task registration and before launcher readiness or
Linux dispatch. A missing, empty, malformed or unsafe file refuses that
configured invocation. Preview and `CheckOnly` remain secret-free. Use
`windows/Test-CreNotificationConfig.ps1` from the collector directory for an
explicit per-machine no-network check; exit `0` proves valid local configuration,
not endpoint authorization or Slack delivery. The
[Slack setup guide](../../../scripts/firecrawl-ops/cre_collector/slack-setup.md)
records the default file location and exact command.
The start message identifies a **Windows attempt**, after option validation and
the initial durable audit, before API readiness and fresh Windows admission.
It explicitly says Linux dispatch has not been attempted. A later readiness or
resource refusal therefore produces a terminal failure with the same attempt ID.
An initial audit or option failure can send only the terminal failure. Parameter
binding failures cannot send. There are no progress or per-listing messages.

Terminal notification follows terminal audit publication, or explicitly reports
that publication failed. The existing `notification` audit field remains the
terminal outcome; `notificationStart` independently records the start outcome,
and `notificationMode` records the selected behavior. Each phase is attempted
at most once. Notification failure preserves the operation's exit code and
never retries the collector or its database writes. A single observed Linux
run/series reference observed on stdout may appear in the terminal message as
informational context; it establishes neither Linux ownership nor settlement.
Conflicting stdout references are omitted; stderr is not a run-reference source.
`CheckOnly` remains quiet and never reads the endpoint in either mode.

For Slack, provision an [Incoming Webhook](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
bound to the intended channel, then
verify one authorized delivery by reading that exact channel. A Codex Slack
connector session is not an unattended Task Scheduler credential. The generic
sender records HTTP acceptance only, not Slack message readback; it does not
implement the authenticated `chat.postMessage` API. No Slack credential or
channel is provisioned by installing these scripts. The native
`Test-CreLifecycleNotification.ps1` and `Test-CreScheduledTask.ps1` fixtures cover
phase ordering, IDs, audit faults, quiet defaults and action serialization
without contacting Slack, providers or the database.

The recommended Windows location is
`$env:USERPROFILE\.config\cre\cre-pipeline.webhook`, containing only the URL.
Keep the file private as described above. The root `.env` configures Docker
Compose; the native scheduled launcher does not load it. Document the file
reference in the task configuration rather than placing the URL in Git. For
another Windows computer, provision its private file and use the same
`$env:USERPROFILE` expression; no hardcoded username or repository edit is needed.

### Credential layout across Windows and Mac

Use `~/.config/cre/` as the private CRE credential directory on each host.
Keep setup instructions and non-secret examples in this repository; provision
the actual values separately on each computer. Do not synchronize credentials
through Git, copy them into `local/machine.md`, or collect unrelated service
keys into one shared environment file.

| File | Format and consumer |
| --- | --- |
| `~/.config/cre/cre-pipeline.webhook` | One URL only, UTF8 without a BOM; Windows uses `-AlertWebhookFile`, while the existing Mac tier wrapper uses `CRE_ALERT_WEBHOOK_FILE`. |
| `~/.config/cre/equire.env` | Approved database `KEY=value` settings; select explicitly with the collector's `--env-file` or `CRE_ENV_FILE`. |
| Repository root `.env` | Optional Docker Compose settings, kept out of Git; this does not configure the native Windows task or automatically load CRE credentials. |

On Windows, `~` means `$env:USERPROFILE`. The database consumer runs inside
Ubuntu, so its `equire.env` belongs in the Linux user's home, currently
`/home/cayman/.config/cre/equire.env`; the Windows notification file belongs in
the Windows user's home. Do not move or replace an existing database file while
adding Slack. Windows webhook files require the owner/ACL checks above. On Mac,
use the Mac user's own home directory, an owned private directory (mode `700`)
and an owned webhook file with mode `600` (or read-only `400`). Preserve existing
values when creating files; never use a setup command that truncates them.

The existing Mac wrapper supports failure alerts through this file format;
Windows `StartFinish` behavior is specific to the native Windows launcher.
Setting a path does not install or enable a Mac schedule. For migration,
provision the needed credentials privately on the destination, update only
non-secret file references, and verify delivery and database identity before
changing the active writer.

The audit records `not_configured`, `not_attempted`, `http_accepted`, `failed`
or `unverified` notification status when writable. Exact HTTP 2xx with native
exit 0 and completed IO means HTTP acceptance, **not destination delivery or
readback**. Notification failure never changes the original launcher exit.
An audit failure still returns its original failure code; when notification
is configured, a later best-effort audit write may retain its explicit
audit-failed status and notification result. Missing initial evidence or a
failed metadata update remains incomplete evidence.

This is not outage paging or cross-run deduplication. Parser/binding failure,
forced launcher death, logout/reboot or an unavailable Windows/network host
can prevent sending. Exact authorized destination binding, scheduled-user
access and an approved delivery/readback test remain operational acceptance
on AGENTIC-3752; no endpoint is configured and no message is sent by installing
this code.

A known, locally settled checkout or credential-file admission refusal can
publish `preflight_refused` and clear its barrier after guards stop and the
terminal receipt is durable, provided no tier worker or uncertain native
command remains. It did not collect or write data. A failure or interruption
after tier dispatch retains the barrier for reconciliation; a busy host or
stale checkout must never be reported as a successful refresh.

| Exit | Meaning |
| --- | --- |
| `0` | Selected trial completed; inspect its scope and receipt |
| `1` | Refusal/failure; inspect receipt and canonical recovery state |
| `73` | Active owner or recovery barrier prevented dispatch |
| `75` | Required resource evidence or pressure prevented/interrupts work |
| `124` | Work deadline expired; inspect settlement and recovery state |
| `130` | Operator interruption; inspect settlement and recovery state |

For a direct Linux foreground trial, use Ctrl+C for cooperative cancellation.
Do not delete lock files or retry around an armed recovery barrier. Follow the
existing checkpoint recovery procedure and reconcile uncertain provider/DB
work before a successor. The Windows launcher's disappearance or Task
Scheduler's **End** action does not prove Linux work stopped:
[Microsoft documents that wslhost can retain its lifetime](https://github.com/microsoft/WSL/blob/master/doc/docs/technical-documentation/wslhost.exe.md).
The independent Linux deadline still applies to a surviving owner. Abrupt
Linux owner death leaves the durable fence armed.

### Cooperative stop for one exact Linux incremental run

Part of [AGENTIC-4163](https://linear.app/agenticassets/issue/AGENTIC-4163).
After a reviewed deployment containing this helper, the one-shot Linux CLI
prints its run directory before starting native work. Copy that exact run ID;
there is no "latest run" selector. Request a stop from Windows with:

```powershell
& .\scripts\firecrawl-ops\cre_collector\windows\Stop-CreGuardedTier.ps1 `
  -RunId '2026-10-13T230000Z-0123456789ab' -WaitSeconds 45
```

The example ID is a placeholder. `-CheckOnly` prints the invocation without
starting WSL or validating an owner. The wrapper forwards to
`python3 cre_stop_guarded_tier.py --run-id ID --wait-seconds 45` in the selected
Ubuntu clone. Stop requests skip Windows CPU admission. Wait is bounded to
1–120 seconds; this does not preempt a blocked kernel call.

New canonical lock directories and their immediate authority parents are
created with owner-only mode 0700, independently of ordinary inherited umasks.
Existing safe parents, including mode 0755, keep their mode and identity.
A symlink, different owner, or group/world-writable immediate parent refuses
before authority files are created or updated; existing unsafe directories are
never chmodded into admission. This boundary covers the immediate parent, not
a recursive permission change to outer ancestors.

The helper checks the private receipt, process birth/boot/namespace/user,
canonical lease and authority inode/token/generation, and the owner's actual
held flock descriptor. It sends one cooperative SIGTERM through a Linux pidfd,
then waits on the same handle for owner exit and matching terminal evidence.
It never signals a bare numeric PID or process group, escalates to SIGKILL,
stops Docker, or changes a recovery barrier. The existing owner remains
responsible for its bounded child-group cleanup and watchdog shutdown.

JSON output distinguishes `completed` from
`terminal_local_settlement_confirmed`, with the actual `terminal_status`,
`signal_sent`, `owner_exited`, `local_settlement`, and `recovery_fence` fields.
Exit 0 requires a matching terminal receipt, stopped watchdogs, and confirmed
local settlement or a verified pre-native `not_started` state. Natural completion racing the request is reported
as completion, without claiming that the request caused cancellation. Exit 1
means refusal before a verified stop; exit 2 means timeout or unverified
terminal/settlement evidence. Inspect the retained receipt and recovery state.
Known interrupted work retains its recovery fence even when local child groups
settled. Local settlement does not prove remote provider cancellation or SQL
rollback, and the helper never clears a fence or authorizes a retry.

Only the actual one-shot Linux process entrypoint publishes this capability.
This capability handles incremental runs only. Imported/library calls, Mac
runs, old receipts and legacy checkpoint series have no retrofitted stop
capability. New opt-in controlled series use the separate
`Stop-CreCheckpointSeries.ps1` helper and the
[series control contract](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md).
Missing pidfd support,
stale or malformed evidence, ownership changes and permission failures refuse
the request. Keep the direct foreground Ctrl+C procedure for Mac; Task Scheduler
End and forced `wsl.exe` termination remain insufficient settlement evidence.

## Windows Task Scheduler installation and acceptance

Before dispatch, the launcher waits at most 90 seconds for an anonymous HTTP
200 response from `http://127.0.0.1:3002/`, with requests capped at two seconds.
It then takes a fresh Windows CPU/memory admission sample. This handles an API
startup race but never starts or restarts Docker. Failure records
`local_api_not_ready_no_dispatch` and starts no Linux work. `-CheckOnly` remains
a resource-telemetry check and makes no API request.

The opt-in installer from [AGENTIC-4265](https://linear.app/agenticassets/issue/AGENTIC-4265)
creates a visible task under `\Agentic Assets\CRE\`, calling the existing
foreground launcher. It uses the current signed-in user's SID, an interactive
token and least privilege, with `IgnoreNew`, missed-start catchup, no wake and
no hard termination. Linux owns the finite work deadline and settlement;
Task Scheduler has no competing execution timeout. Docker Desktop and its
reviewed containers must already be available. The task cannot run while this
user is logged out, and catchup does not prove Docker or API readiness.

Preview an exact source and reviewed, deployed SHA from PowerShell:

```powershell
$taskOptions = @{
    TaskName = 'Interra Refresh'
    Tier = 'series'
    Source = 'interra-realty'
    ExpectedSha = '<full-reviewed-deployed-sha>'
    WorkDeadlineSeconds = 3600
    AcknowledgeDatabaseWrites = $true
    AdmitCurrentInventorySource = 'interra-realty'
    DailyAt = '08:00,20:00'
}
# Optional, after provisioning the private channel webhook file:
# $taskOptions.AlertWebhookFile = (Join-Path $env:USERPROFILE '.config\cre\cre-pipeline.webhook')
# $taskOptions.NotificationMode = 'StartFinish'
& scripts/firecrawl-ops/cre_collector/windows/Install-CreScheduledTask.ps1 @taskOptions
```

Preview starts no task, WSL, provider or database work. `DailyAt` accepts one
to four distinct local `HH:mm` values in a single comma-separated string.
Add `WeeklyOn = 'Saturday'` and use, for example, `DailyAt = '03:00'` for a
weekly task. Each task selects one source, or `all` for a qualified series;
the Interra admission opt-in requires its exact single source. A schedule does
not expand source eligibility or remove baseline and quality requirements.

After the source's supervised live pilot, registry approval and scheduling
acceptance are recorded on AGENTIC-3752, register a disabled task:

```powershell
& scripts/firecrawl-ops/cre_collector/windows/Install-CreScheduledTask.ps1 @taskOptions -Register
```

Adding both `-Register -Enable` instead enables that newly created task only
after its disabled definition passes readback. Registration is create-only:
any existing task collision refuses rather than replacing a definition. To
change a pinned revision or cadence, disable the exact old task, settle any
active Linux work, inspect and unregister that exact task, then preview and
register its replacement. Never auto-fetch/pull code from a scheduled action.

Inspect the actual definition, status and last result from Task Scheduler or
PowerShell:

```powershell
Get-ScheduledTask -TaskPath '\Agentic Assets\CRE\' -TaskName 'Interra Refresh'
Export-ScheduledTask -TaskPath '\Agentic Assets\CRE\' -TaskName 'Interra Refresh'
Get-ScheduledTaskInfo -TaskPath '\Agentic Assets\CRE\' -TaskName 'Interra Refresh'
Disable-ScheduledTask -TaskPath '\Agentic Assets\CRE\' -TaskName 'Interra Refresh'
```

Disabling prevents future launches. For active work, correlate the task run
with `out/windows-launcher/` audit and stream paths, then use the exact
`Stop-CreGuardedTier.ps1` run ID or `Stop-CreCheckpointSeries.ps1` series
directory and invocation ID described above. Task Scheduler **End**, a killed
PowerShell process or a terminated `wsl.exe` does not establish Linux process
settlement or database rollback. Optional failure notifications retain the
separate private-webhook contract above; Task Scheduler history alone is not
an external alert.

Run `Test-CreScheduledTask.ps1` in a fresh PowerShell 7 `-NoProfile` process for
offline definition, quoting and mutation-boundary tests. Its explicit
`-NativeFixture` switch additionally creates and removes one GUID-named local
synthetic task, proving native disabled readback, actual exit propagation and
`IgnoreNew`; it never runs CRE, WSL or a provider. This fixture is not a
production pipeline, login, reboot or notification acceptance test.

Actual scheduled cancellation, startup/login recovery and supervised additive
readback remain acceptance requirements on AGENTIC-3752 and AGENTIC-4141.
No continuously running ChatGPT monitor is needed. Do not schedule the legacy
monitor/enrich/weekly shell commands as a shortcut; completeness uses the
guarded checkpoint-series procedure. The Codex weekly proposal remains a
separately controlled paused alternative. Full51-source acceptance remains
AGENTIC-2902; an installed task does not resolve missing baselines, historical
quality findings or a source's legitimate low inventory count. This Windows
installer makes no Mac scheduler or configuration change.
