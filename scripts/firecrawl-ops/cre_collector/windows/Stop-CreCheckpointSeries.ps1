<#
.SYNOPSIS
Request cooperative shutdown of one exact controlled Linux CRE series.
.DESCRIPTION
Forwards to the owner-bound Linux helper and preserves its exit status. It
does not kill WSL, clear fences, select the latest run, or stop Docker.
-CheckOnly prints the invocation without starting WSL or sending a signal.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^/[^"\r\n]+$')][string]$SeriesDirectory,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{32}$')][string]$InvocationId,
    [ValidateRange(1, 120)][int]$WaitSeconds = 60,
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro = 'Ubuntu-24.04',
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')][string]$LinuxUser = 'cayman',
    [ValidatePattern('^/[^"\r\n]+$')][string]$LinuxRepo = '/home/cayman/code/firecrawl',
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this wrapper on Windows.' }
# Windows PowerShell5.1's native argument marshaller can remove literal double
# quotes. Refuse those paths above rather than alter an exact owner identity.
$collector = $LinuxRepo.TrimEnd('/') + '/scripts/firecrawl-ops/cre_collector'
$arguments = @('-d', $Distro, '-u', $LinuxUser, '--exec', 'python3',
    "$collector/cre_stop_checkpoint_series.py", '--series-dir', $SeriesDirectory,
    '--invocation-id', $InvocationId, '--wait-seconds', "$WaitSeconds")
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
# Stop remains available under high Windows load. Linux validates its exact
# durable receipt and pidfd-bound owner before requesting one cooperative TERM.
$wsl = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop |
    Select-Object -First 1).Source
& $wsl @arguments
exit $LASTEXITCODE
