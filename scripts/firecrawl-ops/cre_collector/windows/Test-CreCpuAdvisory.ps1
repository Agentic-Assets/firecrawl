<# Offline test of the shipped native memory admission and startup decision. #>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Launcher did not parse.' }
foreach ($name in @('Get-CreAdmission', 'Wait-CreStartupAdmission')) {
    $node = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true))[0]
    Invoke-Expression $node.Extent.Text
}
if ($source -match 'GetSystemTimes|Get-CpuTicks|cpu_pressure') { throw 'CPU must not run in startup admission.' }
function Wait-CreLocalApi { param($BudgetSeconds) return @{ ready = $true } }
function Write-CreAudit { param($Path, $Record) }
function Get-CreAvailablePhysicalMemory { return 16GB }
$record = @{}
if (-not (Wait-CreStartupAdmission $record 'unused')) { throw 'Normal memory should admit.' }
if ($record.cpuTelemetry -ne 'not_sampled_at_admission') { throw 'CPU policy must be explicit.' }
function Get-CreAvailablePhysicalMemory { return 1GB }
$record = @{}
if (Wait-CreStartupAdmission $record 'unused') { throw 'Low memory must refuse.' }
if ($record.startupAdmission.reason -ne 'memory_pressure') { throw 'Wrong memory refusal.' }
function Get-CreAvailablePhysicalMemory { throw 'Synthetic unavailable memory' }
try { Wait-CreStartupAdmission @{} 'unused'; throw 'Memory failure was accepted.' }
catch { if ($_.Exception.Message -eq 'Memory failure was accepted.') { throw } }
Write-Output 'Windows startup admission proof passed.'
