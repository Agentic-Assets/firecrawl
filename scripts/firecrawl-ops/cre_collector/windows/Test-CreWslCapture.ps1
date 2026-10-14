<# Opt-in actual WSL argv contract. Uses only a synthetic env/argv reporter;
   never imports a repository module or calls a provider, database or CRE tier.
   Use a fresh Windows PowerShell 5.1 or installed PowerShell 7 -NoProfile -File.
   The Linux command has a 10-second timeout; WSL startup is foreground, not hard-bounded. #>
[CmdletBinding()]
param(
    [switch]$RunWslRoundTrip,
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro = 'Ubuntu-24.04',
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')][string]$LinuxUser = 'cayman',
    [string]$ProofDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $RunWslRoundTrip) {
    Write-Output 'SKIP: actual WSL argv proof requires -RunWslRoundTrip; no dispatch or files.'
    exit 0
}
if ($env:OS -ne 'Windows_NT') { throw 'Actual WSL argv proof requires Windows.' }
if (-not $ProofDirectory) {
    $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/wsl-argv-' + [Guid]::NewGuid().ToString('N'))
}
New-Item -ItemType Directory -Path $ProofDirectory -Force | Out-Null
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$wsl = Join-Path $env:WINDIR 'System32/wsl.exe'
if (-not (Test-Path -LiteralPath $wsl -PathType Leaf)) { throw 'System32 WSL executable is unavailable.' }
$helper = Join-Path $PSScriptRoot 'CreNativeCapture.cs'
$helperBytes = [IO.File]::ReadAllBytes($helper)
$hash = [Security.Cryptography.SHA256]::Create()
try { $helperSha = [BitConverter]::ToString($hash.ComputeHash($helperBytes)).Replace('-', '').ToLowerInvariant() }
finally { $hash.Dispose() }
Add-Type -TypeDefinition ((New-Object Text.UTF8Encoding($false, $true)).GetString($helperBytes))
$script:checks = 0
function Assert-True($condition, [string]$message) {
    if (-not $condition) { throw $message }
    $script:checks++
}
# Reflection preserves actual managed null; PowerShell normally binds null to ''.
$quote = [CreWindowsCapture.Capture].GetMethod('Quote')
foreach ($invalid in @([pscustomobject]@{ Value = $null }, [pscustomobject]@{ Value = 'a' + [char]0 + 'b' })) {
    $refused = $false
    try { $null = $quote.Invoke($null, [object[]]@($invalid.Value)) }
    catch { $refused = $_.Exception.InnerException -is [ArgumentException] }
    Assert-True $refused 'Managed null and NUL must be refused before native dispatch.'
}
$values = @('--synthetic-option', '', 'two words', 'a"b', 'a\"b', 'a\\\"b',
    'C:\space path\', '\\server\path\\', "apostrophe'lit", '&|<>^%PATH%$(literal);`',
    ('caf' + [char]0xe9), [string][char]0x2603, "two`ttabs")
$program = 'import json,os,sys;print(json.dumps({"marker":os.environ.get("CRE_CAPTURE_ARGV_PROBE"),"argv":sys.argv[1:]},ensure_ascii=True))'
$nativeArguments = @('-d', $Distro, '-u', $LinuxUser, '--exec', '/usr/bin/timeout', '10s',
    '/usr/bin/env', 'CRE_CAPTURE_ARGV_PROBE=synthetic value with spaces',
    '/usr/bin/python3', '-c', $program) + $values
$stdout = Join-Path $ProofDirectory 'stdout.log'
$stderr = Join-Path $ProofDirectory 'stderr.log'
$result = [CreWindowsCapture.Capture]::Run($wsl, $nativeArguments, $stdout, $stderr, '/synthetic/no-cre/')
[ordered]@{
    scope = 'actual_wsl_exec_synthetic_argv_only_no_cre_provider_or_database'
    powershellVersion = $PSVersionTable.PSVersion.ToString()
    executable = $wsl
    helperSha256 = $helperSha
    capture = $result
    linuxSettlement = 'not_a_cre_settlement_proof'
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $ProofDirectory 'capture.json') -Encoding UTF8
Assert-True (-not $result.Failed -and $result.NativeExitCode -eq 0) 'Actual WSL option dispatch and argv reporter must return 0 with complete capture.'
Assert-True ($result.Stdout.Eof -and $result.Stderr.Eof) 'Both native streams must reach EOF.'
$actual = [IO.File]::ReadAllText($stdout, [Text.Encoding]::UTF8) | ConvertFrom-Json
Assert-True ($actual.marker -ceq 'synthetic value with spaces') 'Synthetic environment value must survive actual --exec.'
$argv = @($actual.argv)
Assert-True ($argv.Count -eq $values.Count) 'Actual Linux argument count must match.'
for ($i = 0; $i -lt $values.Count; $i++) {
    Assert-True ($argv[$i] -ceq $values[$i]) 'Actual Linux argument value must match literally.'
}
Assert-True ((Get-Item -LiteralPath $stderr).Length -eq 0) 'Synthetic WSL reporter must not emit stderr.'
[ordered]@{ checksPassed = $script:checks; proofDirectory = $ProofDirectory; helperSha256 = $helperSha;
    powershellVersion = $PSVersionTable.PSVersion.ToString(); actualWslArgvCount = $values.Count } | ConvertTo-Json
