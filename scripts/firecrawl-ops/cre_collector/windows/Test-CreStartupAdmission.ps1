<# Offline tests of the actual startup function; no real telemetry, WSL or API. #>
[CmdletBinding()]
param([string]$ProofDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Launcher must parse.' }
$node = @($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Wait-CreStartupAdmission'}, $true))
if ($node.Count -ne 1) { throw 'Expected actual startup admission function.' }
# Replace only its monotonic clock with a deterministic clock advanced by the
# fake API, telemetry and delay below. All admission decisions are shipped code.
$functionText = $node[0].Extent.Text.Replace('[Diagnostics.Stopwatch]::StartNew()', '$script:clock')
. ([scriptblock]::Create($functionText))
$script:checks = 0
function Assert-True($value, [string]$message) { if (-not $value) { throw $message }; $script:checks++ }
function Advance([double]$seconds) {
    $script:clock.Elapsed.TotalSeconds += $seconds
    $script:clock.Elapsed.TotalMilliseconds = $script:clock.Elapsed.TotalSeconds * 1000
}
function Start-Sleep { param([int]$Milliseconds) Advance ($Milliseconds / 1000.0) }
function Wait-CreLocalApi {
    param([double]$BudgetSeconds)
    $script:budgets += $BudgetSeconds
    $step = $script:api[[Math]::Min($script:apiIndex, $script:api.Count - 1)]
    $script:apiIndex++
    Advance $step.Seconds
    return [ordered]@{ready=$step.Ready; attempts=1; scope='synthetic'}
}
function Get-CreAdmission {
    $step = $script:samples[[Math]::Min($script:sampleIndex, $script:samples.Count - 1)]
    $script:sampleIndex++
    Advance $step.Seconds
    if ($step.ContainsKey('Error')) { throw $step.Error }
    return [ordered]@{admitted=($step.Reason -ceq 'admitted'); admissionReason=$step.Reason; observedAt=[DateTime]::UtcNow.ToString('o'); scope='synthetic'}
}
function Write-CreAudit {
    param($Path, $Record)
    if ($script:writeFails) { throw 'Synthetic audit publication failure.' }
    $script:states += $Record.status
}
function Reset-Case {
    $script:clock = [pscustomobject]@{Elapsed=[pscustomobject]@{TotalSeconds=0.0;TotalMilliseconds=0.0}}
    $script:clock | Add-Member ScriptMethod Stop {}
    $script:api = @(@{Ready=$true;Seconds=0.1})
    $script:samples = @(@{Reason='admitted';Seconds=2})
    $script:apiIndex=0; $script:sampleIndex=0; $script:budgets=@(); $script:states=@(); $script:writeFails=$false
    $script:record=[ordered]@{status='admission_pending';dispatch='not_attempted'}
}
Reset-Case
Assert-True (Wait-CreStartupAdmission $record 'unused') 'Ready API and sufficient memory must admit.'
Assert-True ($sampleIndex -eq 1 -and $apiIndex -eq 1 -and $record.admitted) 'Admission uses one API and memory observation.'
Assert-True ($record.startupAdmission.attempts -eq 1 -and $record.startupAdmission.reason -ceq 'admitted') 'Audit records the admission.'
Assert-True ($record.startupAdmission.initialAdmission.admissionReason -ceq 'admitted') 'Audit retains the original observation.'
Assert-True (([DateTimeOffset]::Parse($record.startupAdmission.deadlineAt) - [DateTimeOffset]::Parse($record.startupAdmission.startedAt)).TotalSeconds -eq 90) 'Audit must expose the same finite startup deadline.'
Reset-Case
$script:api=@(@{Ready=$true;Seconds=70})
Assert-True (Wait-CreStartupAdmission $record 'unused') 'API recovering within shared budget may admit after memory observation.'
Assert-True ($clock.Elapsed.TotalSeconds -lt 90 -and $sampleIndex -eq 1) 'API delay consumes shared budget.'
Reset-Case
$script:api=@(@{Ready=$false;Seconds=90})
Assert-True (-not (Wait-CreStartupAdmission $record 'unused')) 'Persistent unavailable API must refuse.'
Assert-True ($sampleIndex -eq 0) 'No telemetry after failed readiness.'
Reset-Case
$script:api=@(@{Ready=$true;Seconds=84})
$script:samples=@(@{Reason='admitted';Seconds=7})
Assert-True (-not (Wait-CreStartupAdmission $record 'unused')) 'A safe sample cannot admit after deadline.'
Assert-True ($record.status -ceq 'startup_admission_deadline_no_dispatch') 'Expired final sample retains no-dispatch reason.'
Reset-Case
$script:samples=@(@{Reason='memory_pressure';Seconds=2})
Assert-True (-not (Wait-CreStartupAdmission $record 'unused')) 'Low RAM remains an immediate refusal.'
Assert-True ($sampleIndex -eq 1 -and $apiIndex -eq 1 -and $record.status -ceq 'admission_refused') 'Low RAM never enters retry loop.'
foreach ($failure in @('missing memory','invalid memory')) {
    Reset-Case
    $script:samples=@(@{Error=$failure;Seconds=2})
    $refused=$false
    try { [void](Wait-CreStartupAdmission $record 'unused') } catch { $refused=$true }
    Assert-True ($refused -and $sampleIndex -eq 1 -and $apiIndex -eq 1) ('Telemetry error must not retry: '+$failure)
}
Reset-Case
$script:writeFails=$true
$refused=$false
try { [void](Wait-CreStartupAdmission $record 'unused') } catch { $refused=$true }
Assert-True ($refused -and $apiIndex -eq 0 -and $sampleIndex -eq 0) 'Audit failure stops before readiness and dispatch.'
# Test the actual unrounded predicate and reason, with only telemetry acquisition
# omitted. This catches classification from rounded presentation numbers.
$admission = @($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Get-CreAdmission'}, $true))[0].Extent.Text
$predicate = $admission.Substring($admission.IndexOf('    $admitted ='))
$predicate = $predicate.Substring(0,$predicate.LastIndexOf('}'))
$evaluate = [scriptblock]::Create('param($bytes) $availablePhysical=$bytes; '+$predicate)
foreach ($case in @(@{Bytes=8GB;Reason='admitted'},@{Bytes=(8GB-1);Reason='memory_pressure'},@{Bytes=0;Reason='memory_pressure'},@{Bytes=16GB;Reason='admitted'})) {
    $result=& $evaluate $case.Bytes
    Assert-True ($result.admissionReason -ceq $case.Reason -and $result.admitted -eq ($case.Reason -ceq 'admitted')) 'Unrounded memory threshold remains exact.'
}
Write-Output ("Startup admission: PASS ({0} assertions; no telemetry, WSL, API or DB calls)" -f $script:checks)
