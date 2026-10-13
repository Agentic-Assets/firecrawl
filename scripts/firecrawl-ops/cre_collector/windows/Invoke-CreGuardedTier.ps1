<#
.SYNOPSIS
Admit one foreground WSL CRE invocation using whole-Windows load evidence.
.DESCRIPTION
Creates no scheduled task and starts no Docker services. -CheckOnly samples
Windows without starting WSL or writing files. Linux owns the durable fence,
deadline and process cleanup. Ending wsl.exe is not proof of Linux settlement.
#>
[CmdletBinding()]
param(
    [ValidateSet('monitor', 'enrich')][string]$Tier = 'monitor',
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')][string]$Source,
    [ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [ValidateRange(1, 50)][int]$Batch = 50,
    [ValidateRange(30, 14400)][int]$DeadlineSeconds = 1800,
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro = 'Ubuntu-24.04',
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')][string]$LinuxUser = 'cayman',
    [ValidatePattern('^/[^\r\n]+$')][string]$LinuxRepo = '/home/cayman/code/firecrawl',
    [ValidatePattern('^/[^\r\n]+$')][string]$LinuxEnvFile = '/home/cayman/.config/cre/equire.env',
    [switch]$Apply,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this launcher on Windows.' }
if (-not $CheckOnly -and (-not $Source -or -not $ExpectedSha)) {
    throw 'Source and ExpectedSha are required to start a tier.'
}

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
$record = [ordered]@{
    observedAt = [DateTime]::UtcNow.ToString('o')
    scope = 'whole_windows_host_start_time_only'
    logicalProcessors = $processorCount
    cpuPercent = [Math]::Round($busy, 2)
    availablePhysicalGiB = [Math]::Round($memory.AvailablePhysical / 1GB, 2)
    maxCpuPercent = 90
    minAvailablePhysicalGiB = 8
    admitted = $admitted
}
$admissionUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
if ($CheckOnly) {
    $record | ConvertTo-Json
    if (-not $admitted) { exit 75 }
    exit 0
}
if (-not $admitted) { throw 'Windows load prevented admission; no WSL tier was started.' }

$wsl = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop |
    Select-Object -First 1).Source
$collector = $LinuxRepo.TrimEnd('/') + '/scripts/firecrawl-ops/cre_collector'
$arguments = @('-d', $Distro, '-u', $LinuxUser, '--exec', 'env',
    'FIRECRAWL_API_URL=http://127.0.0.1:3002', 'python3',
    "$collector/cre_guarded_tier.py", $Tier, '--source', $Source,
    '--expected-sha', $ExpectedSha, '--batch', "$Batch",
    '--deadline-seconds', "$DeadlineSeconds", '--env-file', $LinuxEnvFile,
    '--windows-admission-unix-ms', "$admissionUnixMs")
if ($Apply) { $arguments += '--apply' }

# This is an audit receipt, never a lock or scheduling authority.
$auditDir = Join-Path $PSScriptRoot '../out/windows-launcher'
New-Item -ItemType Directory -Path $auditDir -Force | Out-Null
$auditPath = Join-Path $auditDir (([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) + '-' + [Guid]::NewGuid().ToString('N') + '.json')
$record['tier'] = $Tier
$record['source'] = $Source
$record['expectedSha'] = $ExpectedSha
$record['apply'] = [bool]$Apply
$record['status'] = 'admitted'
$record | ConvertTo-Json | Set-Content -LiteralPath $auditPath -Encoding UTF8
try {
    # Do not detach or force-stop wsl.exe. Linux owns the independent deadline
    # and recovery barrier even if this Windows process unexpectedly disappears.
    & $wsl @arguments
    $result = $LASTEXITCODE
    $record['status'] = 'returned'
    $record['exitCode'] = $result
    $record['returnedAt'] = [DateTime]::UtcNow.ToString('o')
    $record | ConvertTo-Json | Set-Content -LiteralPath $auditPath -Encoding UTF8
    exit $result
} catch {
    $record['status'] = 'windows_launcher_failed_linux_settlement_unverified'
    $record | ConvertTo-Json | Set-Content -LiteralPath $auditPath -Encoding UTF8
    throw
}
