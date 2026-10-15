Governed by [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752), [AGENTIC-4270](https://linear.app/agenticassets/issue/AGENTIC-4270) and [AGENTIC-4279](https://linear.app/agenticassets/issue/AGENTIC-4279).

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
local audit retains the Windows attempt ID and technical run references;
ordinary channel messages lead with the brokerage name and readable outcome.
Wrapper exit zero or an observed stdout reference alone never supplies a
verified listing count.

### What the summary means

`StartFinish` still sends at most two channel messages per Windows attempt.
Start names the brokerage and says preparation checks precede collection.
A qualified single-source series can finish in this form (placeholders):

```text
✅ *Newmark | Completed*
• *Duration:* {duration}
• *Listings verified:* {verified_count} unique properties
• *For sale:* {sale_count} | *For lease:* {lease_count}
• *Both:* {both_count} (included in For sale and For lease)
• *Database readback:* passed; no quality regression.
```

**Listings verified** counts normalized unique listings in that run's committed
generation with exact database readback, not raw provider cards, attempted
requests or newly inserted rows. The notification performs no database query.
Its read-only Linux helper checks safe owned files, matching series/child
control identities, exact Windows admission timestamp, source and reviewed
SHA, successful ingest, final query/readback and no-regression outcomes, and
confirmed local process cleanup. Evidence must be stable and no more than five
minutes past terminal control publication. Inventory-only mixtures and missing
or zero canonical counts currently fall back to an unavailable summary.

Optional **For sale** and **For lease** figures are unique canonical property
IDs normalized from the exact hashed, generation-bound source artifact using
the same `cre_ingest.to_row`, duplicate-identity validation and `merge_rows`
semantics as staging. The normalized identity union must equal both the
artifact's staged-unique count and the committed generation-exact readback.
Counts use each normalized staged row's `transaction_type`, so an explicit
sale-and-lease offering counts in both categories even if it appeared in only
one transaction pass. A property present in both passes also counts in both;
**Both** reports either kind of overlap. These values describe the current
artifact's staged offering classification, not price availability or fresh
property-detail counts. If the optional artifact is missing, unsafe, stale,
oversized, or inconsistent, the split is reported unavailable while the
independently verified total remains. Older schema-v1
summary readers without this optional field remain compatible. **New**,
**updated** and **unchanged** are omitted because the current upsert does not
measure those categories. The duration measures elapsed Windows attempt time,
including startup checks and summary qualification; it is not a source-stage
benchmark. The message makes no full-registry freshness or absolute
database-health claim.

When evidence is missing, stale or contradictory, a zero-returning Windows run
says that the listing refresh could not be verified and points to the local
audit. Failures say the run needs attention; audit publication failure is
identified explicitly. Existing forensic fields remain in
`out/windows-launcher/`, alongside bounded summary-reader output and the
`notificationSummary`/`notificationSummaryStatus` fields when applicable.
Notification or summary-reader failure never retries data work or changes its
exit code. The distinct read-only frontend has a twelve-second native deadline
and the Linux reader a ten-second budget. On expiry, only that separately
spawned read-only frontend can be terminated; the original pipeline frontend is
never terminated or treated as settled by this mechanism. Existing bounded
stream cleanup follows the deadline. Other tiers retain a readable
count-unavailable fallback.

The existing private sender's entire UTF8 JSON payload remains capped at 4096
bytes. There are no progress posts, per-listing logs, automatic alert retries or
extra persistent workers. Technical details stay local. An Incoming Webhook
does not return a message timestamp, so automatic threads need an additional
authorized read/API capability; files also need a separate upload capability.
This implementation adds neither credential nor OAuth scope. See Slack's
[webhook thread contract](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/#posting-your-message-as-a-reply-in-a-thread).

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
