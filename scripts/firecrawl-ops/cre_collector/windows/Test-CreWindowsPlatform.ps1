# Exercise only the launcher platform check and early argument validation; no WSL/task execution.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Launcher has PowerShell parse errors.' }
$guards = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
        $node.Extent.Text.Contains("Run this launcher on Windows.")
}, $true))
if ($guards.Count -ne 1) { throw 'Expected exactly one launcher platform guard.' }
$guardText = $guards[0].Extent.Text
if (-not $guardText.Contains('[Environment]::OSVersion.Platform')) { throw 'Expected platform-native guard.' }
$previous = [Environment]::GetEnvironmentVariable('OS', 'Process')
try {
    foreach ($value in @($null, 'not-windows', 'Windows_NT')) {
        [Environment]::SetEnvironmentVariable('OS', $value, 'Process')
        if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
            $reason = ''
            try { & $launcher -CheckOnly -AttemptsPerSource '2' } catch { $reason = $_.Exception.Message }
            if ($reason -notmatch 'AttemptsPerSource requires series execution') {
                throw "Windows platform guard did not reach early argument validation: $reason"
            }
        }
        $simulatedNonWindows = [ScriptBlock]::Create($guardText.Replace('[Environment]::OSVersion.Platform', '[PlatformID]::Unix'))
        $refused = $false
        try { & $simulatedNonWindows } catch { $refused = $_.Exception.Message -eq 'Run this launcher on Windows.' }
        if (-not $refused) { throw 'Non-Windows platform must be refused despite environment value.' }
    }
} finally {
    [Environment]::SetEnvironmentVariable('OS', $previous, 'Process')
}
Write-Output 'Native platform detection: PASS (missing/misleading OS; non-Windows guard)'
