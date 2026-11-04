<#
.SYNOPSIS
Request cooperative shutdown of one exact guarded Linux CRE tier.
.DESCRIPTION
Forwards to the owner-bound Linux stop helper and preserves its exit status.
It does not kill wsl.exe, inspect numeric PIDs, clear recovery fences, or stop
Docker. -CheckOnly prints the invocation without starting WSL or sending a signal.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z-[0-9a-f]{12}$')]
    [string]$RunId,
    [ValidateRange(1, 120)][int]$WaitSeconds = 45,
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro = 'Ubuntu-24.04',
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')][string]$LinuxUser = 'cayman',
    [ValidatePattern('^/[^\r\n]+$')][string]$LinuxRepo = '/home/cayman/code/firecrawl',
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this wrapper on Windows.' }

$collector = $LinuxRepo.TrimEnd('/') + '/scripts/firecrawl-ops/cre_collector'
$arguments = @('-d', $Distro, '-u', $LinuxUser, '--exec', 'python3',
    "$collector/cre_stop_guarded_tier.py", '--run-id', $RunId,
    '--wait-seconds', "$WaitSeconds")

if ($CheckOnly) {
    [ordered]@{
        scope = 'invocation_only_no_wsl_or_signal'
        executable = 'wsl.exe'
        arguments = $arguments
        ownerValidated = $false
        signalSent = $false
    } | ConvertTo-Json -Depth 3
    exit 0
}

# High host load must not prevent an operator stop. The Linux helper validates
# its exact run receipt and kernel-bound owner before sending one SIGTERM.
$wsl = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop |
    Select-Object -First 1).Source
& $wsl @arguments
exit $LASTEXITCODE
