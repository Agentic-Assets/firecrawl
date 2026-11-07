# Only early validation and stop previews are executed; no task, WSL, or signal.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$names = @('Invoke-CreGuardedTier.ps1', 'Install-CreScheduledTask.ps1',
    'Stop-CreCheckpointSeries.ps1', 'Stop-CreGuardedTier.ps1')
$guards = @{}
foreach ($name in $names) {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $PSScriptRoot $name), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "PowerShell parse errors: $name" }
    $matches = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.IfStatementAst] -and
            $node.Extent.Text -match 'Run this (launcher|installer|wrapper) on Windows\.'
    }, $true))
    if ($matches.Count -ne 1) { throw "Expected exactly one platform guard: $name" }
    $guards[$name] = $matches[0].Extent.Text
    if (-not $guards[$name].Contains('[Environment]::OSVersion.Platform')) { throw "Expected native guard: $name" }
}
$previous = [Environment]::GetEnvironmentVariable('OS', 'Process')
try {
    foreach ($value in @($null, 'not-windows', 'Windows_NT')) {
        [Environment]::SetEnvironmentVariable('OS', $value, 'Process')
        foreach ($name in $names) {
            $simulated = [ScriptBlock]::Create($guards[$name].Replace('[Environment]::OSVersion.Platform', '[PlatformID]::Unix'))
            $refused = $false
            try { & $simulated } catch { $refused = $_.Exception.Message -match '^Run this (launcher|installer|wrapper) on Windows\.$' }
            if (-not $refused) { throw "Non-Windows platform not refused: $name" }
        }
        if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { continue }
        $reason = ''
        try { & (Join-Path $PSScriptRoot $names[0]) -CheckOnly -AttemptsPerSource '2' } catch { $reason = $_.Exception.Message }
        if ($reason -notmatch 'AttemptsPerSource requires series execution') { throw "Launcher early validation not reached: $reason" }
        $reason = ''
        try { & (Join-Path $PSScriptRoot $names[1]) -TaskName 'synthetic' -Tier monitor -ExpectedSha ('a' * 40) -Pipeline } catch { $reason = $_.Exception.Message }
        if ($reason -notmatch 'Pipeline requires series') { throw "Installer early validation not reached: $reason" }
        $hostExe = (Get-Process -Id $PID).Path
        $series = & $hostExe -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot $names[2]) -SeriesDirectory '/synthetic/series' -InvocationId ('a' * 32) -CheckOnly
        if ($LASTEXITCODE -ne 0) { throw 'Series stop preview failed.' }
        $tier = & $hostExe -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot $names[3]) -RunId '2026-11-06T120000Z-aaaaaaaaaaaa' -CheckOnly
        if ($LASTEXITCODE -ne 0) { throw 'Tier stop preview failed.' }
        foreach ($raw in @($series, $tier)) {
            $preview = $raw | ConvertFrom-Json
            if ($preview.scope -ne 'invocation_only_no_wsl_or_signal' -or $preview.ownerValidated -ne $false -or $preview.signalSent -ne $false) {
                throw 'Stop preview claimed authority or sent a signal.'
            }
        }
    }
} finally {
    [Environment]::SetEnvironmentVariable('OS', $previous, 'Process')
}
Write-Output 'Native platform detection: PASS (4 wrappers; missing/misleading/spoofed OS; no dispatch)'
