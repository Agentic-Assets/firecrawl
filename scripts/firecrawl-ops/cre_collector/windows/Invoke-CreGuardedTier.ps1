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
    [ValidatePattern('^[a-z0-9][a-z0-9-]*(,[a-z0-9][a-z0-9-]*)*$')][string]$Sources,
    [switch]$Pipeline,
    [switch]$AdmitBaselineHoldAdditively,
    [ValidateRange(1, 4)][int]$SourceWorkers,
    [ValidateRange(1, 6)][int]$Concurrency = 1,
    [ValidatePattern('^[a-z0-9][a-z0-9-]*(,[a-z0-9][a-z0-9-]*)*$')][string]$AdmitCurrentInventorySources,
    [ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [ValidateRange(1, 50)][int]$Batch = 50,
    [ValidateRange(30, 14400)][int]$DeadlineSeconds = 1800,
    [ValidateRange(30, 86400)][int]$WorkDeadlineSeconds,
    [switch]$AcknowledgeDatabaseWrites,
    [ValidateSet('essex-realty','pyramid-brokerage','interra-realty','faris-lee','fortis-net-lease','unique-properties','kiser-group','pinnacle-rea','cawley-chicago','bradford-allen','hudson-peters','gibson-commercial','leibsohn','nai-hiffman','nai-martens','bull-realty','tri-commercial','berger-commercial','nai-bergman','nai-isaac','trinity-partners','metro-commercial','33-realty','nai-hallmark','nai-plotkin','greysteel','nai-talcor','nai-dominion')][string]$AdmitCurrentInventorySource,
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

# Pure selection validation runs before readiness, telemetry or notification work.
if ($Sources -and ($Source -or -not $Pipeline -or $Tier -ne 'series')) {
    throw '-Sources requires pipeline series and conflicts with -Source.'
}
if ($PSBoundParameters.ContainsKey('Concurrency') -and $Tier -ne 'series') {
    throw '-Concurrency requires series.'
}
if ($Pipeline -and $Tier -ne 'series') { throw '-Pipeline requires series.' }
if ($PSBoundParameters.ContainsKey('SourceWorkers') -and -not $Pipeline) {
    throw '-SourceWorkers requires -Pipeline.'
}
$resolvedSourceWorkers = 1
if ($Pipeline) {
    $resolvedSourceWorkers = 2
    if ($PSBoundParameters.ContainsKey('SourceWorkers')) { $resolvedSourceWorkers = $SourceWorkers }
}
$knownSourceKeys = @()
if ($Pipeline) {
$policyForSelection = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../data/cre-source-policy.json') -Raw | ConvertFrom-Json
$knownSourceKeys = @($policyForSelection.PSObject.Properties.Name)
}
$selectedSourceKeys = @()
if ($Sources) {
    $selectedSourceKeys = @($Sources.Split(','))
    if (@($selectedSourceKeys | Sort-Object -Unique).Count -ne $selectedSourceKeys.Count) { throw 'Duplicate source keys.' }
    foreach ($key in $selectedSourceKeys) {
        if ($knownSourceKeys -cnotcontains $key) { throw 'Unknown source key.' }
    }
} elseif ($Source -ceq 'all' -and $Pipeline) { $selectedSourceKeys = $knownSourceKeys }
elseif ($Source) { $selectedSourceKeys = @($Source) }
if ($AdmitCurrentInventorySource -and $AdmitCurrentInventorySources) { throw 'Conflicting admission flags.' }
$selectedAdmissionKeys = @()
if ($AdmitCurrentInventorySources) {
    if (-not $Pipeline) { throw 'Multiple admissions require pipeline mode.' }
    $selectedAdmissionKeys = @($AdmitCurrentInventorySources.Split(','))
} elseif ($AdmitCurrentInventorySource) { $selectedAdmissionKeys = @($AdmitCurrentInventorySource) }
if (@($selectedAdmissionKeys | Sort-Object -Unique).Count -ne $selectedAdmissionKeys.Count) { throw 'Duplicate admission keys.' }
$admissibleKeys = $selectedAdmissionKeys
if ($AdmitCurrentInventorySources) {
$buildoutRows = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../data/cre-buildout-sources.json') -Raw | ConvertFrom-Json
$admissibleKeys = @('interra-realty') + @($buildoutRows | ForEach-Object { $_[0] })
}
foreach ($key in $selectedAdmissionKeys) {
    if ($Tier -ne 'series' -or $admissibleKeys -cnotcontains $key -or $selectedSourceKeys -cnotcontains $key) {
        throw 'Admission requires an exact selected and supported source key.'
    }
}
if ($AdmitBaselineHoldAdditively -and ($Tier -ne 'series' -or $Source -cne 'savills' -or $Sources -or $Pipeline -or $selectedAdmissionKeys.Count -ne 0)) {
    throw '-AdmitBaselineHoldAdditively requires a Savills-only serial series without other admissions.'
}
$selectedSourceArgument = if ($Sources) { $Sources } else { $Source }

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
    $reason = if ($memory.AvailablePhysical -lt 8GB) { 'memory_pressure' }
        elseif ($busy -ge 90) { 'cpu_pressure' } else { 'admitted' }
    return [ordered]@{
        observedAt = [DateTime]::UtcNow.ToString('o')
        scope = 'whole_windows_host_start_time_only'
        logicalProcessors = $processorCount
        cpuPercent = [Math]::Round($busy, 2)
        availablePhysicalGiB = [Math]::Round($memory.AvailablePhysical / 1GB, 2)
        maxCpuPercent = 90
        minAvailablePhysicalGiB = 8
        admitted = $admitted
        admissionReason = $reason
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

function Wait-CreStartupAdmission {
    param([System.Collections.IDictionary]$Record, [string]$AuditPath)
    # One startup budget, not a retry of the launcher or dispatched Linux work.
    $budgetSeconds = 90
    $startedAt = [DateTime]::UtcNow
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $startup = [ordered]@{ budgetSeconds=$budgetSeconds; attempts=0;
        startedAt=$startedAt.ToString('o'); deadlineAt=$startedAt.AddSeconds($budgetSeconds).ToString('o');
        cpuPressureSamples=0; elapsedMilliseconds=0; reason='readiness_pending'; initialAdmission=$null }
    $Record['startupAdmission'] = $startup
    try {
        while ($clock.Elapsed.TotalSeconds -lt $budgetSeconds) {
            $Record.status = 'startup_admission_pending'
            $startup.elapsedMilliseconds = [Math]::Round($clock.Elapsed.TotalMilliseconds)
            Write-CreAudit $AuditPath $Record
            $remaining = $budgetSeconds - $clock.Elapsed.TotalSeconds
            if ($remaining -lt 0.1) { break }
            $Record['localApiReadiness'] = Wait-CreLocalApi -BudgetSeconds $remaining
            if (-not $Record.localApiReadiness.ready) {
                $Record.status = 'local_api_not_ready_no_dispatch'
                $startup.reason = 'api_not_ready'
                return $false
            }
            # A valid CPU sample can take up to five seconds. Never start one
            # without that reserve or admit if it finishes after the deadline.
            if (($budgetSeconds - $clock.Elapsed.TotalSeconds) -lt 5) { break }
            $startup.attempts++
            try { $admission = Get-CreAdmission }
            catch { $startup.reason = 'telemetry_invalid'; throw }
            if ($startup.attempts -eq 1) { $startup.initialAdmission = $admission }
            foreach ($key in $admission.Keys) { $Record[$key] = $admission[$key] }
            $startup.elapsedMilliseconds = [Math]::Round($clock.Elapsed.TotalMilliseconds)
            if ($clock.Elapsed.TotalSeconds -ge $budgetSeconds) { break }
            if ($admission.admitted) {
                $startup.reason = 'admitted'
                return $true
            }
            # This reason comes from the actual unrounded telemetry predicate.
            # Low memory and telemetry exceptions never enter the wait loop.
            if ($admission.admissionReason -cne 'cpu_pressure') {
                $Record.status = 'admission_refused'
                $startup.reason = $admission.admissionReason
                return $false
            }
            $startup.cpuPressureSamples++
            $startup.reason = 'cpu_pressure'
            $Record.status = 'startup_admission_waiting'
            Write-CreAudit $AuditPath $Record
            $delay = [int][Math]::Min(1000, [Math]::Max(0,
                ($budgetSeconds - $clock.Elapsed.TotalSeconds) * 1000))
            if ($delay -gt 0) { Start-Sleep -Milliseconds $delay }
        }
        $Record.status = 'startup_admission_deadline_no_dispatch'
        $startup.reason = 'startup_deadline'
        return $false
    } finally {
        $clock.Stop()
        $startup.elapsedMilliseconds = [Math]::Round($clock.Elapsed.TotalMilliseconds)
    }
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

function Get-CreBrokerageName {
    param([string]$Key)
    $names = @{
        'newmark'='Newmark'; 'interra-realty'='Interra Realty'; 'hanley'='Hanley Investment Group'
        'cbre'='CBRE'; 'cbre-dealflow'='CBRE Deal Flow'; 'jll'='JLL'; 'jll-investor'='JLL Investor Center'
        'cushman-wakefield'='Cushman & Wakefield'; 'nai-global'='NAI Global'; 'srs'='SRS'
        'svn'='SVN'; 'lee-associates'='Lee & Associates'; 'pinnacle-rea'='Pinnacle Real Estate Advisors'
    }
    if ($Key -and $names.ContainsKey($Key)) { return $names[$Key] }
    if ($Key -match '^[a-z0-9][a-z0-9-]{0,79}$') {
        $genericName = [Globalization.CultureInfo]::InvariantCulture.TextInfo.ToTitleCase($Key.Replace('-', ' '))
        return $genericName -creplace '\bNai\b', 'NAI' -creplace '\bCbre\b', 'CBRE' -creplace '\bJll\b', 'JLL' -creplace '\bSrs\b', 'SRS'
    }
    return 'Commercial real estate'
}

function Get-CreVerifiedSummary {
    param([System.Collections.IDictionary]$Record, [string]$AttemptId,
        [ValidateRange(50,12000)][int]$ReadDeadlineMilliseconds = 12000)
    $pipelineMode = [bool](Get-Variable -Name Pipeline -ValueOnly -ErrorAction SilentlyContinue)
    $notificationSource = Get-Variable -Name selectedSourceArgument -ValueOnly -ErrorAction SilentlyContinue
    if (-not $notificationSource) { $notificationSource = $Source }

    # The stdout reference selects a location only. Linux verifies the private
    # terminal owner/child receipts, exact attempt identity and database readback.
    if ($Tier -ne 'series' -or $Record.status -ne 'returned' -or
        ($Record.nativeExitCode -ne 0 -and -not $pipelineMode) -or -not $Record.Contains('capture') -or
        -not $Record.Contains('windowsAdmissionUnixMs')) { return $null }
    $capture = $Record.capture
    if ($capture.Failed -or $capture.Stdout.RunReferenceConflict -or $capture.Stderr.RunReferenceConflict) { return $null }
    $directory = $capture.Stdout.ObservedRunDirectory
    if (-not $directory -or ($capture.Stderr.ObservedRunDirectory -and
        $capture.Stderr.ObservedRunDirectory -cne $directory)) { return $null }
    $collector = $LinuxRepo.TrimEnd('/') + '/scripts/firecrawl-ops/cre_collector'
    if ($directory -cnotmatch ('^' + [Regex]::Escape($collector + '/out/checkpoint-series/') + '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z$')) { return $null }
    $wsl = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $summaryOut = Join-Path $auditDir ($AttemptId + '.summary.json')
    $summaryError = Join-Path $auditDir ($AttemptId + '.summary.stderr.log')
    $summaryArgs = @('-d', $Distro, '-u', $LinuxUser, '--exec', 'timeout', '--signal=TERM', '--kill-after=2s', '10s',
        'python3', "$collector/cre_notification_summary.py", '--series-directory', $directory,
        '--expected-sha', $ExpectedSha,
        '--windows-admission-unix-ms', [string]$Record.windowsAdmissionUnixMs)
    if ($pipelineMode) { $summaryArgs += @('--pipeline-source-keys', ($selectedSourceKeys -join ',')) }
    else { $summaryArgs += @('--source', $notificationSource) }
    $summaryCapture = [CreWindowsCapture.Capture]::RunReadOnly($wsl, $summaryArgs, $summaryOut, $summaryError, $ReadDeadlineMilliseconds)
    $Record['notificationSummaryCapture'] = $summaryCapture
    if ($summaryCapture.Failed -or $summaryCapture.NativeExitCode -ne 0 -or
        $summaryCapture.Stdout.Truncated -or $summaryCapture.Stdout.ObservedBytes -gt $(if ($pipelineMode) { 32768 } else { 4096 }) -or
        $summaryCapture.Stderr.ObservedBytes -ne 0) { return $null }
    $summary = [IO.File]::ReadAllText($summaryOut) | ConvertFrom-Json
    if ($pipelineMode) {
        try {
            if ($summary.schema_version -ne 1 -or $summary.verified -isnot [bool] -or -not $summary.verified -or
                $summary.complete -isnot [bool] -or $summary.collector_git_sha -cne $ExpectedSha -or
                $summary.windows_admission_unix_ms -ne $Record.windowsAdmissionUnixMs -or
                $summary.series_id -cne ($directory.Split('/')[-1]) -or
                $summary.invocation_id -cnotmatch '^[0-9a-f]{32}$' -or
                $summary.run_id -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z-[0-9a-f]{12}$' -or
                $summary.local_settlement -cne 'confirmed' -or
                ($summary.selected_source_keys -join ',') -cne ($selectedSourceKeys -join ',')) { return $null }
            $verifiedKeys = @()
            foreach ($entry in @($summary.per_source_summaries)) {
                if ($selectedSourceKeys -cnotcontains $entry.source_key -or
                    -not ($entry.verified_listings -is [int] -or $entry.verified_listings -is [long]) -or
                    $entry.verified_listings -lt 0 -or $entry.verified_listings -gt 500000) { return $null }
                $breakdownProperty = $entry.PSObject.Properties['transaction_breakdown']
                if ($null -ne $breakdownProperty) {
                    $breakdown = $breakdownProperty.Value
                    try {
                        $validBreakdown = $null -ne $breakdown -and
                            $breakdown.measurement -is [string] -and
                            $breakdown.measurement -ceq 'current_staged_inventory_transaction_types' -and
                            (($breakdown.sale -is [long]) -or ($breakdown.sale -is [int])) -and
                            (($breakdown.lease -is [long]) -or ($breakdown.lease -is [int])) -and
                            (($breakdown.both -is [long]) -or ($breakdown.both -is [int])) -and
                            $breakdown.sale -ge 0 -and $breakdown.lease -ge 0 -and $breakdown.both -ge 0 -and
                            $breakdown.both -le $breakdown.sale -and $breakdown.both -le $breakdown.lease -and
                            ($breakdown.sale + $breakdown.lease - $breakdown.both) -eq $entry.verified_listings
                    } catch { $validBreakdown = $false }
                    if (-not $validBreakdown) { $entry.transaction_breakdown = $null }
                }
                $verifiedKeys += $entry.source_key
            }
            $partition = @($verifiedKeys) + @($summary.failed_source_keys) + @($summary.pending_source_keys)
            if (@($partition | Sort-Object -Unique).Count -ne $partition.Count -or
                (($partition | Sort-Object) -join ',') -cne (($selectedSourceKeys | Sort-Object) -join ',') -or
                $summary.complete -ne ($verifiedKeys.Count -eq $selectedSourceKeys.Count)) { return $null }
        } catch { return $null }
        $Record['notificationSummary'] = $summary
        return $summary
    }
    foreach ($field in @('source','measurement','collector_git_sha','series_id','invocation_id','run_id','local_settlement')) {
        if ($summary.$field -isnot [string]) { return $null }
    }
    if (-not ($summary.schema_version -is [int] -or $summary.schema_version -is [long]) -or
        $summary.schema_version -ne 1 -or $summary.verified -isnot [bool] -or -not $summary.verified -or
        $summary.source -cne $notificationSource -or $summary.measurement -cne 'generation_exact_current_inventory' -or
        $summary.collector_git_sha -cne $ExpectedSha -or $summary.windows_admission_unix_ms -ne $Record.windowsAdmissionUnixMs -or
        $summary.series_id -cne ($directory.Split('/')[-1]) -or $summary.invocation_id -cnotmatch '^[0-9a-f]{32}$' -or
        $summary.run_id -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z-[0-9a-f]{12}$' -or
        $summary.local_settlement -cne 'confirmed' -or $summary.quality_no_regression -isnot [bool] -or -not $summary.quality_no_regression -or
        -not ($summary.verified_listings -is [long] -or $summary.verified_listings -is [int]) -or
        $summary.verified_listings -le 0 -or $summary.verified_listings -gt 500000 -or
        ($summary.transaction_scope -join ',') -cne 'sale,lease') { return $null }
    if ((Get-Variable -Name AdmitBaselineHoldAdditively -ValueOnly -ErrorAction SilentlyContinue) -and ($summary.PSObject.Properties['whole_source_coverage'] -eq $null -or
        $summary.whole_source_coverage -isnot [bool] -or $summary.whole_source_coverage -or
        $summary.PSObject.Properties['baseline_hold_retained'] -eq $null -or $summary.baseline_hold_retained -isnot [bool])) { return $null }
    $breakdownProperty = $summary.PSObject.Properties['transaction_breakdown']
    if ($null -ne $breakdownProperty) {
        $breakdown = $breakdownProperty.Value
        try {
            $validBreakdown = $null -ne $breakdown -and
                $breakdown.measurement -is [string] -and
                $breakdown.measurement -ceq 'current_staged_inventory_transaction_types' -and
                (($breakdown.sale -is [long]) -or ($breakdown.sale -is [int])) -and
                (($breakdown.lease -is [long]) -or ($breakdown.lease -is [int])) -and
                (($breakdown.both -is [long]) -or ($breakdown.both -is [int])) -and
                $breakdown.sale -ge 0 -and $breakdown.lease -ge 0 -and $breakdown.both -ge 0 -and
                $breakdown.both -le $breakdown.sale -and $breakdown.both -le $breakdown.lease -and
                ($breakdown.sale + $breakdown.lease - $breakdown.both) -eq $summary.verified_listings
        } catch { $validBreakdown = $false }
        if (-not $validBreakdown) { $summary.transaction_breakdown = $null }
    }
    $Record['notificationSummary'] = $summary
    return $summary
}

function Get-CreNotificationText {
    param([System.Collections.IDictionary]$Record, [string]$Phase, [int]$ExitCode, $Summary)
    $pipelineMode = [bool](Get-Variable -Name Pipeline -ValueOnly -ErrorAction SilentlyContinue)
    $notificationSource = Get-Variable -Name selectedSourceArgument -ValueOnly -ErrorAction SilentlyContinue
    if (-not $notificationSource) { $notificationSource = $Source }

    $name = (Get-CreBrokerageName $notificationSource).Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
    $bullet = [char]0x2022
    if ($Phase -eq 'Start') {
        $started = [char]::ConvertFromUtf32(0x1F680)
        return "`n$started *$name | Started*`n`n$bullet Preparation checks are running on the Windows desktop; collection has not started."
    }
    $duration = 'unavailable'
    try {
        $elapsed = [DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($Record.createdAt, [Globalization.CultureInfo]::InvariantCulture)
        if ($elapsed.TotalSeconds -ge 0 -and $elapsed.TotalHours -le 25) {
            $duration = if ($elapsed.TotalHours -ge 1) { '{0}h {1}m' -f [int][Math]::Floor($elapsed.TotalHours), $elapsed.Minutes }
                elseif ($elapsed.TotalMinutes -ge 1) { '{0}m {1}s' -f [int][Math]::Floor($elapsed.TotalMinutes), $elapsed.Seconds }
                else { '{0}s' -f [int][Math]::Floor($elapsed.TotalSeconds) }
        }
    } catch { }
    if ($pipelineMode -and $null -ne $Summary -and $null -ne $Summary.PSObject.Properties['per_source_summaries']) {
        $title = if ($Summary.complete -and $ExitCode -eq 0) { 'Completed' } else { 'Needs attention' }
        $statusMark = if ($Summary.complete -and $ExitCode -eq 0) { [char]::ConvertFromUtf32(0x2705) }
            else { [char]::ConvertFromUtf32(0x26A0) + [char]0xFE0F }
        $lines = @('', "$statusMark *CRE pipeline | $title*", '', "$bullet *Duration:* $duration", '')
        $members = @($Summary.per_source_summaries)
        $firstMember = $true
        foreach ($entry in @($members | Select-Object -First 20)) {
            if (-not $firstMember) { $lines += '' }
            $firstMember = $false
            $broker = (Get-CreBrokerageName $entry.source_key).Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
            $count = ([long]$entry.verified_listings).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
            $breakdownProperty = $entry.PSObject.Properties['transaction_breakdown']
            if ($null -ne $breakdownProperty -and $null -ne $breakdownProperty.Value) {
                $sale = ([long]$breakdownProperty.Value.sale).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
                $lease = ([long]$breakdownProperty.Value.lease).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
                $both = ([long]$breakdownProperty.Value.both).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
                $lines += "$bullet *${broker}:* $count listings verified by database readback`n    *For sale:* $sale | *For lease:* $lease | *Both:* $both"
            } else {
                $lines += "$bullet *${broker}:* $count listings verified by database readback`n    *For sale / lease / both:* unavailable"
            }
        }
        if ($members.Count -gt 20) {
            $lines += ''
            $lines += "$bullet $($members.Count - 20) additional verified brokerages are listed in the local CRE audit."
        }
        $lines += ''
        $lines += "$bullet Both counts overlap and are included in the For sale and For lease figures."
        $lines += ''
        $lines += "$bullet *Failed:* $(@($Summary.failed_source_keys).Count) | *Pending:* $(@($Summary.pending_source_keys).Count)"
        if (-not $Summary.complete -or $ExitCode -ne 0) {
            $lines += ''
            $lines += "$bullet Partial result; the selected refresh did not finish successfully. Check the local CRE audit."
        }
        return (($lines -join "`n").Substring(0, [Math]::Min(4000, ($lines -join "`n").Length)))
    }
    if ($ExitCode -ne 0) {
        if ($Record.status -eq 'windows_terminal_audit_failed_linux_settlement_unverified') {
            $warning = [char]::ConvertFromUtf32(0x26A0) + [char]0xFE0F
            return "`n$warning *$name | Needs attention*`n`n$bullet *Duration:* $duration`n$bullet The run's audit could not be saved; completion is unverified. Check the local CRE audit."
        }
        $warning = [char]::ConvertFromUtf32(0x26A0) + [char]0xFE0F
        return "`n$warning *$name | Needs attention*`n`n$bullet *Duration:* $duration`n$bullet The Windows run did not finish successfully. Check the local CRE audit; verified listing counts are unavailable."
    }
    if ($null -ne $Summary) {
        $count = ([long]$Summary.verified_listings).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
        $breakdownProperty = $Summary.PSObject.Properties['transaction_breakdown']
        $breakdown = if ($null -ne $breakdownProperty) { $breakdownProperty.Value } else { $null }
        if ($null -ne $breakdown) {
            $sale = ([long]$breakdown.sale).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
            $lease = ([long]$breakdown.lease).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
            $both = ([long]$breakdown.both).ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
            $transactionLine = "$bullet *For sale:* $sale | *For lease:* $lease"
            if ([long]$breakdown.both -gt 0) { $transactionLine += "`n$bullet *Both:* $both (included in For sale and For lease)" }
        } else {
            $transactionLine = "$bullet *For sale:* unavailable | *For lease:* unavailable (breakdown not verified)"
        }
        $coverageLine = ''
        if ($null -ne $Summary.PSObject.Properties['baseline_hold_retained'] -and $Summary.baseline_hold_retained -eq $true) {
            $coverageLine = "`n$bullet *Coverage:* historical baseline hold retained; additive refresh only."
        }
        $success = [char]::ConvertFromUtf32(0x2705)
        return "`n$success *$name | Completed*`n`n$bullet *Duration:* $duration`n$bullet *Listings verified:* $count unique properties`n$transactionLine`n$bullet *Database readback:* passed; no quality regression.$coverageLine"
    }
    $warning = [char]::ConvertFromUtf32(0x26A0) + [char]0xFE0F
    return "`n$warning *$name | Could not be verified*`n`n$bullet *Duration:* $duration`n$bullet Verified listing total and sale/lease counts are unavailable. Check the local CRE audit for details."
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
        $summary = $null
        if ($Phase -eq 'Terminal' -and $ExitCode -eq 0) {
            $Record['notificationSummaryStatus'] = 'unavailable'
            try { $summary = Get-CreVerifiedSummary $Record $AttemptId }
            catch { $Record['notificationSummaryUnavailable'] = $true }
            if ($null -ne $summary) { $Record['notificationSummaryStatus'] = 'verified' }
        }
        $text = Get-CreNotificationText $Record $Phase $ExitCode $summary
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
    admitBaselineHoldAdditively = [bool]$AdmitBaselineHoldAdditively
    tier = $Tier; source = $selectedSourceArgument; pipeline = [bool]$Pipeline; sourceWorkers = $resolvedSourceWorkers; expectedSha = $ExpectedSha; apply = [bool]$Apply
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
    if (-not $selectedSourceArgument -or -not $ExpectedSha) { throw 'Required invocation identity missing.' }
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
    if (-not (Wait-CreStartupAdmission $record $auditPath)) {
        # Preserve ordinary launch refusal exit 1; CheckOnly still uses 75.
    } else {
        $record.status = 'admitted'
        $admissionUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $record['windowsAdmissionUnixMs'] = $admissionUnixMs
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
            "$collector/cre_guarded_tier.py", $Tier, '--source', $selectedSourceArgument,
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
                '--sources', $selectedSourceArgument, '--concurrency', [string]$Concurrency, '--nice', '15',
                '--page-cap', '400', '--attempts-per-source', '3', '--max-resource-recoveries-per-source', '0',
                '--max-host-cpu-percent', '90', '--cpu-sustain-seconds', '30', '--cpu-sample-seconds', '2',
                '--memory-sustain-seconds', '20', '--memory-sample-seconds', '2',
                '--expected-sha', $ExpectedSha, '--windows-admission-unix-ms', "$admissionUnixMs",
                '--work-deadline-seconds', "$WorkDeadlineSeconds", '--env-file', $LinuxEnvFile)
            if ($Pipeline) { $arguments += @('--pipeline', '--source-workers', "$resolvedSourceWorkers") }
            if ($AdmitBaselineHoldAdditively) { $arguments += '--admit-baseline-hold-additively' }
            foreach ($key in $selectedAdmissionKeys) { $arguments += @('--admit-current-inventory-source', $key) }
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
