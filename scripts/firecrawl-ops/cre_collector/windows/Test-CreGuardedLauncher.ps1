# Tests the shipped executable-resolution expression without starting WSL.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$launcher = Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $launcher, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Launcher has PowerShell parse errors.' }
$assignments = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left.Extent.Text -eq '$wsl'
}, $true))
if ($assignments.Count -ne 2) { throw 'Expected dispatch and read-only summary WSL executable resolutions.' }
foreach ($assignment in $assignments) {
$expression = [ScriptBlock]::Create($assignment.Right.Extent.Text)

foreach ($count in @(1, 2)) {
    & {
        param($expression, $count)
        function Get-Command {
            param($Name, $CommandType, $ErrorAction)
            [pscustomobject]@{ Source = 'C:\first\wsl.exe' }
            if ($count -eq 2) { [pscustomobject]@{ Source = 'C:\second\wsl.exe' } }
        }
        $selected = & $expression
        if ($selected -isnot [string] -or $selected -cne 'C:\first\wsl.exe') {
            throw 'WSL resolution must return exactly the first executable path.'
        }
    } $expression $count
}

& {
    param($expression)
    function Get-Command {
        param($Name, $CommandType, $ErrorAction)
        throw 'synthetic missing executable'
    }
    $refused = $false
    try { & $expression | Out-Null } catch { $refused = $true }
    if (-not $refused) { throw 'Missing WSL must fail closed.' }
} $expression
}
Write-Output 'PowerShell launcher resolution: PASS (dispatch + read-only summary; one path, two paths, missing executable)'
