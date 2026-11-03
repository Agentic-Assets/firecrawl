# Internal foreground task step. It requires the running WSL lock owner.
param(
 [Parameter(Mandatory=$true)][ValidateSet('Inspect','Hold','VerifyHeld','Repin','Restore','VerifyRestored','ReconcileHold','VerifyOriginal','CompleteRepin','CompleteRestore')][string]$Stage,
 [Parameter(Mandatory=$true)][string]$Plan,
 [Parameter(Mandatory=$true)][string]$PlanSha,
 [Parameter(Mandatory=$true)][string]$Control,
 [Parameter(Mandatory=$true)][string]$ControlSha,
 [Parameter(Mandatory=$true)][int]$OwnerPid,
 [Parameter(Mandatory=$true)][string]$Generation,
 [Parameter(Mandatory=$true)][string]$Output,
 [Parameter(Mandatory=$true)][string]$FailureOutput
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreReviewedTaskUpdate.ps1')
$planPath=ConvertTo-CreHandoffPath $Plan
$outputPath=ConvertTo-CreHandoffPath $Output
if($PlanSha -cnotmatch '^[0-9a-f]{64}$' -or -not (Test-Path -LiteralPath $planPath -PathType Leaf) -or
   (Get-CreReviewedControlHash $planPath) -cne $PlanSha) {throw 'Current update inputs changed'}
$planObject=Read-CreHandoffJson ([IO.File]::ReadAllText($planPath))
$release=$planObject.release
function AssertOwner {
 if((Get-CreReviewedControlHash $planPath) -cne $PlanSha) {throw 'Current update inputs changed'}
 Assert-CreReviewedOwner $release $Control $ControlSha $OwnerPid $Generation
}
try {
 $snapshot=if($Stage -ceq 'Inspect'){$null}else{@($planObject.snapshot)}
 $digest=if($Stage -ceq 'Inspect'){''}else{[string]$planObject.digest}
 $result=Invoke-CreReviewedTaskStage $Stage $release $snapshot ([string]$planObject.old_sha) ([string]$planObject.new_sha) $digest ${function:AssertOwner}
 AssertOwner
 Save-CreHandoffExclusive $outputPath ($result|ConvertTo-Json -Depth 30 -Compress)
} catch {
 $original=$_
 try {
  AssertOwner
  $failurePath=ConvertTo-CreHandoffPath $FailureOutput
  $receipt=@{stage=$Stage;plan_sha256=$PlanSha;owner_pid=$OwnerPid;generation_sha256=(Get-CreHandoffHash $Generation);completed=$true}
  Save-CreHandoffExclusive $failurePath ($receipt|ConvertTo-Json -Compress)
 } catch {}
 throw $original
}
