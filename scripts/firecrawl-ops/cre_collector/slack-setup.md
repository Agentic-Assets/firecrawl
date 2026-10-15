Governed by [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752) and [AGENTIC-4270](https://linear.app/agenticassets/issue/AGENTIC-4270).

# CRE pipeline Slack setup

The destination is **Agentic Assets / `#cre-pipeline`** (workspace
`T0AFPVB3HU2`, channel `C0C9K5SCWRY`), using the **Agentic Assets Alerts**
Slack app. The intended Windows task behavior is one start and one terminal
success/failure message per scheduled invocation, with no per-listing chatter
or continuous ChatGPT monitor. Channel existence and a working webhook do not
by themselves prove that a scheduled task is configured or enabled.

## Private credential file

Save **only the Incoming Webhook URL** on the first line of this file:

| Host and consumer | File |
| --- | --- |
| Windows native scheduled launcher | `%USERPROFILE%\.config\cre\cre-pipeline.webhook` |
| Mac tier wrapper, if separately configured | `$HOME/.config/cre/cre-pipeline.webhook` |

Do not add a variable name, `KEY=`, quotes, a JSON wrapper, or additional
credentials. Save as UTF8 without a BOM, with at most one final newline.
On Windows the file must be owned by the scheduled user and grant access only
to that user, SYSTEM and Administrators. On Mac the existing wrapper requires
an owned readable file with mode `600` or `400`; use a private directory.
The [notification contract](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md#optional-windows-notifications)
documents the exact Windows checks, transport bounds and audit fields.

The webhook is a credential. Keep it outside every Git checkout and out of
task arguments, screenshots, logs and documentation. Store only its file path
in configuration. Docker's root `.env` is a separate consumer and does not
configure the Windows task. Database credentials stay in the existing private
`equire.env`; on this Windows PC that file is inside the Ubuntu user's home.
See the [credential layout](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md#credential-layout-across-windows-and-mac).

## Check configuration before scheduling

From the repository root on Windows, run the explicit no-network check:

```powershell
pwsh -NoProfile -File scripts/firecrawl-ops/cre_collector/windows/Test-CreNotificationConfig.ps1
```

This computer-specific check must fail for a missing, empty, invalid or unsafe
webhook file. It must not print the URL or send a message. Passing proves local
configuration only. Ordinary offline unit tests use private synthetic files
and do not require a real Slack credential.

For a reviewed Windows task configuration, provide the **path**, not the URL:

```powershell
$taskOptions.AlertWebhookFile = Join-Path $env:USERPROFILE '.config\cre\cre-pipeline.webhook'
$taskOptions.NotificationMode = 'StartFinish'
```

Use the existing [guarded task installation procedure](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md#windows-task-scheduler-installation-and-acceptance)
for the other required options and exact reviewed SHA. These two assignments
alone do not install, enable or run a task. The existing Mac wrapper accepts
`CRE_ALERT_WEBHOOK_FILE` and supports failure alerts; Windows `StartFinish`
behavior is not automatically added to Mac schedules. Preserve the sole-writer
handoff and leave the Mac disabled while Windows owns production.

## Verify delivery and interpret messages

Provision the destination through Slack's [Incoming Webhook setup](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/).
The connector used for interactive chat is separate from the credential an
unattended Windows task needs. For another computer, provision a private file
under that computer's own user account, update its non-secret file reference,
and repeat local configuration and authorized delivery checks.

A successful HTTP response is transport acceptance. Read the exact intended
channel and match the test's unique identifier before claiming delivery. The
Windows attempt ID correlates start and finish messages with the local audit.
A terminal message reports wrapper/native outcomes; an observed Linux run
reference is informational. Inspect the actual checkpoint, validation and
database readback before claiming source completion or database freshness.

The first private-file delivery was verified on October 15, 2026: the sender
returned HTTP `200`, exited normally, and the exact unique test message was
read back in `#cre-pipeline` through the Slack connector. [Delivery receipt](https://agentic-assets.slack.com/archives/C0C9K5SCWRY/p1792082430340859).
This is a dated setup result, not evidence of current task enablement or a
completed scheduled pipeline run. Current operational acceptance lives on
AGENTIC-4270 and AGENTIC-4265.

If configuration fails, fix the private file and its permissions without
relaxing the checks. If delivery fails, inspect the redacted notification
audit and Slack app/channel authorization. Never rerun database work merely
to retry an alert. Forced process death, logout, reboot or a network outage
can prevent a terminal notification; Slack is concise visibility, not a
guaranteed outage-paging service.
