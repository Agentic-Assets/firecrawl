<#
.SYNOPSIS
Admit one foreground WSL CRE invocation using whole-Windows load evidence.
.DESCRIPTION
Creates no scheduled task and starts no Docker services. -CheckOnly samples
Windows without starting WSL or writing files. Linux owns the durable fence,
deadline and process cleanup. Ending wsl.exe is not proof of Linux settlement.
Series mode performs additive database writes and requires a separate explicit
work budget and acknowledgement; it has no dry-run mode.
#>
[CmdletBinding()]
param(
    [ValidateSet('monitor', 'enrich', 'series')][string]$Tier = 'monitor',
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')][string]$Source,
    [ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [ValidateRange(1, 50)][int]$Batch = 50,
    [ValidateRange(30, 14400)][int]$DeadlineSeconds = 1800,
    [ValidateRange(30, 86400)][int]$WorkDeadlineSeconds,
    [switch]$AcknowledgeDatabaseWrites,
    [ValidateSet('interra-realty')][string]$AdmitCurrentInventorySource,
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro = 'Ubuntu-24.04',
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')][string]$LinuxUser = 'cayman',
    [ValidatePattern('^/[^\r\n]+$')][string]$LinuxRepo = '/home/cayman/code/firecrawl',
    [ValidatePattern('^/[^\r\n]+$')][string]$LinuxEnvFile = '/home/cayman/.config/cre/equire.env',
    [string]$AlertWebhookFile,
    [ValidateSet('Failure', 'StartFinish')][string]$NotificationMode = 'Failure',
    [switch]$Apply,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this launcher on Windows.' }
function Get-CpuTicks {
    $idle = New-Object CreWindowsAdmission.Native+FileTime
    $kernel = New-Object CreWindowsAdmission.Native+FileTime
    $user = New-Object CreWindowsAdmission.Native+FileTime
    if (-not [CreWindowsAdmission.Native]::GetSystemTimes([ref]$idle, [ref]$kernel, [ref]$user)) {
        throw 'Whole-Windows CPU telemetry is unavailable.'
    }
    return @([CreWindowsAdmission.Native]::Value($idle),
        [CreWindowsAdmission.Native]::Value($kernel), [CreWindowsAdmission.Native]::Value($user))
}

function Get-CreAdmission {
    if (-not ('CreWindowsAdmission.Native' -as [type])) {
        Add-Type -TypeDefinition @'
    using System;
    using System.Runtime.InteropServices;
    namespace CreWindowsAdmission {
        public static class Native {
            [StructLayout(LayoutKind.Sequential)]
            public struct FileTime { public uint Low; public uint High; }
            [StructLayout(LayoutKind.Sequential)]
            public struct MemoryStatus {
                public uint Length; public uint Load;
                public ulong TotalPhysical; public ulong AvailablePhysical;
                public ulong TotalPageFile; public ulong AvailablePageFile;
                public ulong TotalVirtual; public ulong AvailableVirtual;
                public ulong AvailableExtendedVirtual;
            }
            [DllImport("kernel32.dll", SetLastError=true)]
            public static extern bool GetSystemTimes(out FileTime idle, out FileTime kernel, out FileTime user);
            [DllImport("kernel32.dll", SetLastError=true)]
            public static extern bool GlobalMemoryStatusEx(ref MemoryStatus status);
            [DllImport("kernel32.dll", SetLastError=true)]
            public static extern uint GetActiveProcessorCount(ushort group);
            public static ulong Value(FileTime time) { return ((ulong)time.High << 32) | time.Low; }
        }
    }
'@
    }

    # GetSystemTimes covers one processor group above 64 processors. Refuse that
    # unsupported topology rather than claiming a whole-host measurement.
    $processorCount = [CreWindowsAdmission.Native]::GetActiveProcessorCount(65535)
    if ($processorCount -eq 0 -or $processorCount -gt 64) { throw 'Whole-host CPU admission requires 1 to 64 logical processors.' }
    $sampleClock = [Diagnostics.Stopwatch]::StartNew()
    $before = Get-CpuTicks
    Start-Sleep -Seconds 2
    $after = Get-CpuTicks
    $sampleClock.Stop()
    if ($sampleClock.Elapsed.TotalSeconds -gt 5) { throw 'Windows CPU observation is stale.' }
    $delta = @(0, 0, 0)
    for ($i = 0; $i -lt 3; $i++) {
        if ($after[$i] -lt $before[$i]) { throw 'Windows CPU counters regressed.' }
        $delta[$i] = [double]($after[$i] - $before[$i])
    }
    $total = $delta[1] + $delta[2] # kernel includes idle
    if ($total -le 0 -or $delta[0] -gt $total) { throw 'Windows CPU counters are invalid.' }
    $busy = 100.0 * ($total - $delta[0]) / $total
    if ([double]::IsNaN($busy) -or [double]::IsInfinity($busy) -or $busy -lt 0 -or $busy -gt 100) {
        throw 'Whole-Windows CPU percentage is invalid.'
    }
    $memory = New-Object CreWindowsAdmission.Native+MemoryStatus
    $memory.Length = [Runtime.InteropServices.Marshal]::SizeOf($memory)
    if (-not [CreWindowsAdmission.Native]::GlobalMemoryStatusEx([ref]$memory) -or
        $memory.TotalPhysical -eq 0 -or $memory.AvailablePhysical -gt $memory.TotalPhysical) {
        throw 'Whole-Windows memory telemetry is unavailable or invalid.'
    }
    $admitted = $busy -lt 90 -and $memory.AvailablePhysical -ge 8GB
    return [ordered]@{
        observedAt = [DateTime]::UtcNow.ToString('o')
        scope = 'whole_windows_host_start_time_only'
        logicalProcessors = $processorCount
        cpuPercent = [Math]::Round($busy, 2)
        availablePhysicalGiB = [Math]::Round($memory.AvailablePhysical / 1GB, 2)
        maxCpuPercent = 90
        minAvailablePhysicalGiB = 8
        admitted = $admitted
    }
}

function Wait-CreLocalApi {
    param(
        [ValidateRange(0.1,90)][double]$BudgetSeconds = 90,
        [ValidateRange(1,2000)][int]$RequestTimeoutMilliseconds = 2000,
        [ValidateRange(1,1000)][int]$RetryDelayMilliseconds = 1000,
        [uri]$Endpoint = 'http://127.0.0.1:3002/'
    )
    # Private test parameters permit disposable loopback fixtures. Production
    # calls this function without overrides; no proxy, redirects or credentials.
    if ($Endpoint.Scheme -cne 'http' -or $Endpoint.Host -cne '127.0.0.1' -or
        $Endpoint.UserInfo -or $Endpoint.AbsolutePath -cne '/' -or
        $Endpoint.Query -or $Endpoint.Fragment) { throw 'Local API probe endpoint is invalid.' }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $handler.UseDefaultCredentials = $false
    $handler.UseCookies = $false
    $client = New-Object Net.Http.HttpClient($handler)
    $client.Timeout = [Threading.Timeout]::InfiniteTimeSpan
    $attempts = 0
    $lastStatus = $null
    $ready = $false
    try {
        while ($clock.Elapsed.TotalSeconds -lt $BudgetSeconds) {
            $remainingMs = ($BudgetSeconds - $clock.Elapsed.TotalSeconds) * 1000
            $timeoutMs = [int][Math]::Max(1, [Math]::Min($RequestTimeoutMilliseconds, $remainingMs))
            $cancel = New-Object Threading.CancellationTokenSource
            $request = New-Object Net.Http.HttpRequestMessage([Net.Http.HttpMethod]::Get, $Endpoint)
            $response = $null
            $attempts++
            try {
                $cancel.CancelAfter($timeoutMs)
                $response = $client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead,
                    $cancel.Token).GetAwaiter().GetResult()
                $lastStatus = [int]$response.StatusCode
                $ready = $lastStatus -eq 200 -and $clock.Elapsed.TotalSeconds -le $BudgetSeconds
            } catch {
                # A startup transport failure is retried within the same finite
                # budget. Never print exception text or inherit proxy auth.
                $lastStatus = $null
            } finally {
                if ($null -ne $response) { $response.Dispose() }
                $request.Dispose()
                $cancel.Dispose()
            }
            if ($ready) { break }
            $delayMs = [int][Math]::Min($RetryDelayMilliseconds,
                [Math]::Max(0, ($BudgetSeconds - $clock.Elapsed.TotalSeconds) * 1000))
            if ($delayMs -gt 0) { Start-Sleep -Milliseconds $delayMs }
        }
    } finally { $client.Dispose(); $handler.Dispose(); $clock.Stop() }
    return [ordered]@{ ready=$ready; observedAt=[DateTime]::UtcNow.ToString('o');
        attempts=$attempts; elapsedMilliseconds=[Math]::Round($clock.Elapsed.TotalMilliseconds);
        budgetSeconds=$BudgetSeconds; requestTimeoutMilliseconds=$RequestTimeoutMilliseconds;
        lastHttpStatus=$lastStatus; scope='local_api_root_only' }
}

function Write-CreAudit {
    param([string]$Path, [System.Collections.IDictionary]$Record)
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $bytes = (New-Object Text.UTF8Encoding($false, $true)).GetBytes(($Record | ConvertTo-Json -Depth 8) + "`n")
        if ($bytes.Length -gt 65536) { throw 'Windows audit exceeds its byte limit.' }
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

$script:notificationAttempted = @{ Start = $false; Terminal = $false }
function Set-CreNotification {
    param([System.Collections.IDictionary]$Record, [string]$AttemptId, [int]$ExitCode,
        [ValidateSet('Start','Terminal')][string]$Phase = 'Terminal')
    $field = if ($Phase -eq 'Start') { 'notificationStart' } else { 'notification' }
    if ($Phase -eq 'Start' -and $NotificationMode -ne 'StartFinish') { return }
    if ($Phase -eq 'Terminal' -and $ExitCode -eq 0 -and $NotificationMode -eq 'Failure') {
        $Record[$field] = [ordered]@{ Status = 'not_attempted'; Reason = 'successful_invocation' }
        return
    }
    if ($script:notificationAttempted[$Phase]) { return }
    $script:notificationAttempted[$Phase] = $true
    if (-not $AlertWebhookFile) {
        $Record[$field] = [ordered]@{ Status = 'not_configured'; Reason = 'not_configured' }
        return
    }
    try {
        $notificationPath = Join-Path $PSScriptRoot 'CreWindowsNotification.cs'
        $notificationBytes = [IO.File]::ReadAllBytes($notificationPath)
        $hash = [Security.Cryptography.SHA256]::Create()
        try { $notificationSha = [BitConverter]::ToString($hash.ComputeHash($notificationBytes)).Replace('-', '').ToLowerInvariant() }
        finally { $hash.Dispose() }
        if ('CreWindowsNotification.Sender' -as [type]) {
            if (-not ('CreWindowsNotification.Identity' -as [type]) -or [CreWindowsNotification.Identity]::Sha256 -cne $notificationSha) { throw 'Cached notification helper identity differs.' }
        } else {
            $notificationSource = (New-Object Text.UTF8Encoding($false, $true)).GetString($notificationBytes)
            Add-Type -TypeDefinition ($notificationSource + "`nnamespace CreWindowsNotification { public static class Identity { public const string Sha256 = ""$notificationSha""; } }")
        }
        $curl = (Get-Command curl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $safeSource = if ($Source -match '^[a-z0-9][a-z0-9-]{0,79}$') { $Source } else { 'unspecified' }
        $nativeCode = if ($null -eq $Record.nativeExitCode) { 'unobserved' } else { [string][int]$Record.nativeExitCode }
        if ($Phase -eq 'Start') {
            $text = "CRE Windows attempt $AttemptId started (tier=$Tier source=$safeSource). Linux dispatch has not been attempted."
        } else {
            $outcome = if ($ExitCode -eq 0) { 'finished' } else { 'failed' }
            $text = "CRE Windows attempt $AttemptId $outcome (tier=$Tier source=$safeSource wrapper_exit=$ExitCode native_exit=$nativeCode status=$($Record.status)). Linux settlement is unverified; inspect local audit and recovery evidence."
            if ($Record.Contains('capture') -and $null -ne $Record.capture -and
                -not $Record.capture.Stdout.RunReferenceConflict -and $Record.capture.Stdout.ObservedRunDirectory -and
                $Record.capture.Stdout.ObservedRunDirectory -match '/([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z(?:-[0-9a-f]{12})?)\z') {
                $text += " Observed Linux run/series reference (informational): $($Matches[1])."
            }
        }
        $payload = [ordered]@{ text = $text } | ConvertTo-Json -Compress
        $repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
        $Record[$field] = [CreWindowsNotification.Sender]::Send($AlertWebhookFile, $repository, $curl, $payload)
    } catch {
        $Record[$field] = [ordered]@{ Status = 'failed'; Reason = 'notification_unavailable'; NativeSettlement = 'unverified' }
    }
}

if ($CheckOnly) {
    try {
        $admission = Get-CreAdmission
        $admission | ConvertTo-Json
        if (-not $admission.admitted) { exit 75 }
        exit 0
    } catch { Write-Error 'Windows admission telemetry failed.' -ErrorAction Continue; exit 1 }
}

# This audit is informational, never Linux ownership or settlement authority.
$auditDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../out/windows-launcher'))
$basename = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N')
$auditPath = Join-Path $auditDir ($basename + '.json')
$stdoutPath = Join-Path $auditDir ($basename + '.stdout.log')
$stderrPath = Join-Path $auditDir ($basename + '.stderr.log')
$record = [ordered]@{
    createdAt = [DateTime]::UtcNow.ToString('o')
    tier = $Tier; source = $Source; expectedSha = $ExpectedSha; apply = [bool]$Apply
    status = 'admission_pending'; dispatch = 'not_attempted'
    nativeExitCode = $null; wrapperExitCode = $null; linuxSettlement = 'unverified'
    notificationMode = $NotificationMode
    notificationStart = [ordered]@{ Status = 'not_attempted'; Reason = 'invocation_pending' }
    notification = [ordered]@{ Status = 'not_attempted'; Reason = 'invocation_pending' }
    stdoutPath = $stdoutPath; stderrPath = $stderrPath; byteLimitPerStream = 1048576
}
if ($Tier -eq 'series') {
    $record['workDeadlineSeconds'] = $WorkDeadlineSeconds
    $record['acknowledgeDatabaseWrites'] = [bool]$AcknowledgeDatabaseWrites
}
try {
    New-Item -ItemType Directory -Path $auditDir -Force | Out-Null
    Write-CreAudit $auditPath $record
} catch {
    Write-Error 'Windows initial audit publication failed; no dispatch attempted.' -ErrorAction Continue
    $record.status = 'windows_initial_audit_failed_linux_settlement_unverified'
    $record.wrapperExitCode = 1
    Set-CreNotification $record $basename 1
    if ($AlertWebhookFile) { try { Write-CreAudit $auditPath $record } catch { } }
    exit 1
}
Write-Output "Windows audit: $auditPath"
Write-Output "Windows stdout: $stdoutPath"
Write-Output "Windows stderr: $stderrPath"
$result = 1
try {
    if (-not $Source -or -not $ExpectedSha) { throw 'Required invocation identity missing.' }
    if ($AdmitCurrentInventorySource -and ($Tier -ne 'series' -or $Source -cne $AdmitCurrentInventorySource)) {
        throw 'Current-inventory admission requires its exact Interra series source.'
    }
    if ($Tier -eq 'series') {
        if (-not $PSBoundParameters.ContainsKey('WorkDeadlineSeconds') -or -not $AcknowledgeDatabaseWrites) {
            throw 'Series requires an explicit work budget and database-write acknowledgement.'
        }
        foreach ($incompatible in @('Apply', 'Batch', 'DeadlineSeconds')) {
            if ($PSBoundParameters.ContainsKey($incompatible)) { throw 'Incremental-only option supplied for series.' }
        }
    } elseif ($PSBoundParameters.ContainsKey('WorkDeadlineSeconds') -or $PSBoundParameters.ContainsKey('AcknowledgeDatabaseWrites')) {
        throw 'Series-only option supplied for incremental tier.'
    }
    if ($NotificationMode -eq 'StartFinish' -and -not $AlertWebhookFile) {
        throw 'Lifecycle notification requires a private webhook-file reference.'
    }
    if ($NotificationMode -eq 'StartFinish') {
        & (Join-Path $PSScriptRoot 'Test-CreNotificationConfig.ps1') -AlertWebhookFile $AlertWebhookFile -Quiet
        $record['notificationConfig'] = [ordered]@{ configured=($LASTEXITCODE -eq 0); networkCalled=$false }
        if (-not $record.notificationConfig.configured) {
            $record.status = 'notification_config_refused_no_dispatch'
            throw 'Private notification configuration is invalid.'
        }
    }
    Set-CreNotification $record $basename 0 -Phase Start
    $record.status = 'local_api_readiness_pending'
    Write-CreAudit $auditPath $record
    $record['localApiReadiness'] = Wait-CreLocalApi
    if (-not $record.localApiReadiness.ready) {
        $record.status = 'local_api_not_ready_no_dispatch'
        throw 'Local API readiness deadline expired.'
    }
    $admission = Get-CreAdmission
    foreach ($key in $admission.Keys) { $record[$key] = $admission[$key] }
    if (-not $admission.admitted) {
        $record.status = 'admission_refused'
        # Preserve ordinary launch refusal exit 1; CheckOnly still uses 75.
    } else {
        $record.status = 'admitted'
        $admissionUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $wsl = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop |
            Select-Object -First 1).Source
        $helperPath = Join-Path $PSScriptRoot 'CreNativeCapture.cs'
        # Hash exactly the bytes decoded/compiled, and reject a stale cached type
        # when invoked from a reusable host. Prefer a fresh -NoProfile -File host.
        $helperBytes = [IO.File]::ReadAllBytes($helperPath)
        $hash = [Security.Cryptography.SHA256]::Create()
        try { $helperSha = [BitConverter]::ToString($hash.ComputeHash($helperBytes)).Replace('-', '').ToLowerInvariant() }
        finally { $hash.Dispose() }
        $helperSource = (New-Object Text.UTF8Encoding($false, $true)).GetString($helperBytes)
        if ('CreWindowsCapture.Capture' -as [type]) {
            if (-not ('CreWindowsCapture.Identity' -as [type]) -or [CreWindowsCapture.Identity]::Sha256 -cne $helperSha) {
                throw 'Cached capture helper identity differs.'
            }
        } else {
            Add-Type -TypeDefinition ($helperSource + "`nnamespace CreWindowsCapture { public static class Identity { public const string Sha256 = ""$helperSha""; } }")
        }
        $record['captureHelperSha256'] = $helperSha
        $hash = [Security.Cryptography.SHA256]::Create()
        try {
            $record['launcherFileSha256Observed'] = [BitConverter]::ToString($hash.ComputeHash([IO.File]::ReadAllBytes($PSCommandPath))).Replace('-', '').ToLowerInvariant()
        } finally { $hash.Dispose() }
        $collector = $LinuxRepo.TrimEnd('/') + '/scripts/firecrawl-ops/cre_collector'
        $arguments = @('-d', $Distro, '-u', $LinuxUser, '--exec', 'env',
            'FIRECRAWL_API_URL=http://127.0.0.1:3002', 'python3',
            "$collector/cre_guarded_tier.py", $Tier, '--source', $Source,
            '--expected-sha', $ExpectedSha, '--batch', "$Batch",
            '--deadline-seconds', "$DeadlineSeconds", '--env-file', $LinuxEnvFile,
            '--windows-admission-unix-ms', "$admissionUnixMs")
        $runPrefix = "$collector/out/guarded-tiers/"
        if ($Tier -eq 'series') {
            $arguments = @('-d', $Distro, '-u', $LinuxUser, '--exec', 'env',
                'FIRECRAWL_API_URL=http://127.0.0.1:3002',
                'JLL_DETAIL_CONCURRENCY=1', 'JLL_INVESTOR_DETAIL_CONCURRENCY=1',
                'CUSHMAN_DETAIL_CONCURRENCY=1', 'COLLIERS_MAIN_COVEO_ENABLE=1',
                'COLLIERS_MAIN_DETAIL_CONCURRENCY=1', 'COLLIERS_MAIN_DETAIL_START_INTERVAL_MS=3000',
                'COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS=60000', 'NAI_ENUMERATION_CONCURRENCY=1',
                'python3', "$collector/cre_checkpoint_series.py",
                '--sources', $Source, '--concurrency', '1', '--nice', '15',
                '--page-cap', '400', '--attempts-per-source', '3', '--max-resource-recoveries-per-source', '0',
                '--max-host-cpu-percent', '90', '--cpu-sustain-seconds', '30', '--cpu-sample-seconds', '2',
                '--memory-sustain-seconds', '20', '--memory-sample-seconds', '2',
                '--expected-sha', $ExpectedSha, '--windows-admission-unix-ms', "$admissionUnixMs",
                '--work-deadline-seconds', "$WorkDeadlineSeconds", '--env-file', $LinuxEnvFile)
            if ($AdmitCurrentInventorySource) { $arguments += @('--admit-current-inventory-source', $AdmitCurrentInventorySource) }
            $runPrefix = "$collector/out/checkpoint-series/"
        } elseif ($Apply) { $arguments += '--apply' }
        # Pending is durably published before helper entry. An exception/crash
        # thereafter may have dispatched; it never proves no Linux work started.
        $record.dispatch = 'pending'
        Write-CreAudit $auditPath $record
        # No detach, kill or forced WSL exit. Linux owns its independent deadline.
        $capture = [CreWindowsCapture.Capture]::Run($wsl, $arguments, $stdoutPath, $stderrPath,
            $runPrefix)
        $record['capture'] = $capture
        if ($capture.StartAttempted) { $record.dispatch = 'attempted' }
        if ($null -ne $capture.NativeExitCode) {
            $record.dispatch = 'returned'
            $record.nativeExitCode = $capture.NativeExitCode
            $record['exitCode'] = $capture.NativeExitCode # Existing audit compatibility.
            $record['returnedAt'] = [DateTime]::UtcNow.ToString('o')
        }
        if ($capture.Failed) { $record.status = 'windows_capture_failed_linux_settlement_unverified' }
        else { $record.status = 'returned'; $result = [int]$capture.NativeExitCode }
    }
} catch {
    # Fixed diagnostics: never dump child argv, environment or exception text.
    if ($record.status -eq 'local_api_not_ready_no_dispatch') {
        Write-Error 'Local Firecrawl API was not ready within the startup budget; no Linux work was dispatched.' -ErrorAction Continue
    } elseif ($record.status -eq 'notification_config_refused_no_dispatch') {
        Write-Error 'Private Windows notification configuration is invalid; no Linux work was dispatched.' -ErrorAction Continue
    } else {
        $record.status = 'windows_launcher_failed_linux_settlement_unverified'
        Write-Error 'Windows admission or launcher failed; inspect the audit and Linux recovery state.' -ErrorAction Continue
    }
}
$record.wrapperExitCode = $result
if ($NotificationMode -eq 'Failure' -and ($result -eq 0 -or -not $AlertWebhookFile)) {
    Set-CreNotification $record $basename $result
}
try { Write-CreAudit $auditPath $record }
catch {
    Write-Error 'Windows terminal audit publication failed; prior receipt remains incomplete.' -ErrorAction Continue
    $result = 1
    $record.wrapperExitCode = $result
    $record.status = 'windows_terminal_audit_failed_linux_settlement_unverified'
}
if ($result -ne 0 -or $NotificationMode -eq 'StartFinish') {
    Set-CreNotification $record $basename $result
    # Notification publication is best effort and never changes the tier exit.
    if ($AlertWebhookFile) { try { Write-CreAudit $auditPath $record } catch { } }
}
exit $result
