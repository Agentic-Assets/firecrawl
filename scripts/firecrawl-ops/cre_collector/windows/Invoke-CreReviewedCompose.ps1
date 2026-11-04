# Foreground Compose step; its receipt proves native process completion.
param(
 [Parameter(Mandatory=$true)][string]$Plan,
 [Parameter(Mandatory=$true)][string]$PlanSha,
 [Parameter(Mandatory=$true)][string]$Control,
 [Parameter(Mandatory=$true)][string]$ControlSha,
 [Parameter(Mandatory=$true)][int]$OwnerPid,
 [Parameter(Mandatory=$true)][string]$Generation,
 [Parameter(Mandatory=$true)][string]$Receipt
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreReviewedTaskUpdate.ps1')
$planPath=ConvertTo-CreHandoffPath $Plan
$receiptPath=ConvertTo-CreHandoffPath $Receipt
if($PlanSha -cnotmatch '^[0-9a-f]{64}$' -or (Get-CreReviewedControlHash $planPath) -cne $PlanSha) {throw 'Compose plan changed'}
$planObject=Read-CreHandoffJson ([IO.File]::ReadAllText($planPath))
$release=$planObject.release
$mode='up'
if($null -ne $planObject.PSObject.Properties['mode']) {$mode=[string]$planObject.mode}
if($mode -cnotin @('up','rollback')) {throw 'Unsupported Compose mode'}
$docker=ConvertTo-CreHandoffPath ([string]$planObject.docker)
$dockerSha=[string]$planObject.docker_sha256
if($dockerSha -cnotmatch '^[0-9a-f]{64}$' -or (Get-CreReviewedControlHash $docker) -cne $dockerSha) {throw 'Docker executable changed'}
$argsList=@($planObject.argv | ForEach-Object {[string]$_})
if($argsList.Count -lt 12 -or $argsList[0] -cne '--host' -or
   $argsList[1] -cne 'npipe:////./pipe/dockerDesktopLinuxEngine' -or
   $argsList[2] -cne 'compose' -or $argsList[3] -cne '--project-name' -or
   $argsList[4] -cne 'firecrawl' -or $argsList[5] -cne '--project-directory' -or
   $argsList[6] -ine [string]$release.roots.native) {throw 'Unsupported Compose command'}
$index=7;$files=0;$fileArgs=@()
while($index -lt $argsList.Count -and $argsList[$index] -ceq '-f') {
 if($index+1 -ge $argsList.Count) {throw 'Compose file missing'}
 $fileArgs+=@(ConvertTo-CreHandoffPath $argsList[$index+1])
 $files++;$index+=2
}
if($files -lt 1 -or $index+6 -ge $argsList.Count -or
   $argsList[$index] -cne 'up' -or $argsList[$index+1] -cne '-d' -or
   $argsList[$index+2] -cne '--no-deps' -or $argsList[$index+3] -cne '--no-build' -or
   $argsList[$index+4] -cne '--pull' -or $argsList[$index+5] -cne 'never') {throw 'Unsupported Compose operation'}
$services=@($argsList[($index+6)..($argsList.Count-1)])
if($services.Count -eq 0 -or @($services|Select-Object -Unique).Count -ne $services.Count -or
   @($services|Where-Object {$_ -cnotin @('api','playwright-service')}).Count) {throw 'Unsupported Compose services'}
$imagePlanPath=ConvertTo-CreHandoffPath ([string]$planObject.image_plan)
$imagePlanSha=[string]$planObject.image_plan_sha256
if($imagePlanSha -cnotmatch '^[0-9a-f]{64}$' -or (Get-CreReviewedControlHash $imagePlanPath) -cne $imagePlanSha) {throw 'Image plan changed'}
$imagePlan=Read-CreHandoffJson ([IO.File]::ReadAllText($imagePlanPath))
if($imagePlan.selected.sha -cne $planObject.selected.sha -or $imagePlan.selected.tree -cne $planObject.selected.tree) {throw 'Selected source differs'}
$fileRows=@($planObject.files)
if($fileRows.Count -ne $fileArgs.Count) {throw 'Compose file binding differs'}
for($i=0;$i -lt $fileRows.Count;$i++) {
 if([IO.Path]::GetFullPath([string]$fileRows[$i].path) -ine [IO.Path]::GetFullPath([string]$fileArgs[$i]) -or
    [string]$fileRows[$i].sha256 -cnotmatch '^[0-9a-f]{64}$') {throw 'Compose file binding differs'}
}
$imageRows=@($planObject.images)
if($imageRows.Count -ne $services.Count) {throw 'Image binding count differs'}
foreach($service in $services) {
 if($mode -ceq 'up') {
  $matching=@($imageRows|Where-Object {$_.tag -cmatch ('^firecrawl-'+[regex]::Escape($service)+':qa-reviewed-[0-9a-f]{24}$')})
 } else {
  $prior=$imagePlan.previous_images.PSObject.Properties[$service].Value
  if($null -eq $prior) {throw 'Previous image binding missing'}
  $matching=@($imageRows|Where-Object {$_.tag -ceq [string]$prior.ref -and $_.image -ceq [string]$prior.image})
 }
 if($matching.Count -ne 1 -or [string]$matching[0].image -cnotmatch '^sha256:[0-9a-f]{64}$') {throw 'Image binding differs'}
}
if($mode -ceq 'rollback') {
 $priorFiles=@($imagePlan.compose_stack.files)
 $priorHashes=@($imagePlan.compose_stack.sha256)
 if($priorFiles.Count -ne $fileRows.Count -or $priorHashes.Count -ne $fileRows.Count) {throw 'Previous Compose stack differs'}
 for($i=0;$i -lt $fileRows.Count;$i++) {
  if([IO.Path]::GetFullPath([string]$priorFiles[$i]) -ine [IO.Path]::GetFullPath([string]$fileRows[$i].path) -or
     [string]$priorHashes[$i] -cne [string]$fileRows[$i].sha256) {throw 'Previous Compose stack differs'}
 }
}
function AssertOwner {
 if((Get-CreReviewedControlHash $planPath) -cne $PlanSha -or
    (Get-CreReviewedControlHash $docker) -cne $dockerSha -or
    (Get-CreReviewedControlHash $imagePlanPath) -cne $imagePlanSha) {throw 'Compose inputs changed'}
 foreach($file in $fileRows) {
  if((Get-CreReviewedControlHash ([string]$file.path)) -cne [string]$file.sha256) {throw 'Compose file changed'}
 }
 Assert-CreReviewedOwner $release $Control $ControlSha $OwnerPid $Generation
}
function AssertImages {
 foreach($image in $imageRows) {
  $actual=& $docker --host 'npipe:////./pipe/dockerDesktopLinuxEngine' image inspect --format '{{.Id}}' ([string]$image.tag)
  if($LASTEXITCODE -ne 0 -or @($actual).Count -ne 1 -or [string]$actual -cne [string]$image.image) {throw 'Prepared image tag changed'}
 }
}
AssertOwner
AssertImages
AssertOwner
& $docker @argsList
$dockerExit=$LASTEXITCODE
AssertOwner
AssertImages
AssertOwner
$stage=if($mode -ceq 'rollback') {'ComposeRollback'} else {'ComposeUp'}
$completion=@{stage=$stage;plan_sha256=$PlanSha;owner_pid=$OwnerPid;
 generation_sha256=(Get-CreHandoffHash $Generation);completed=$true;docker_exit=[int]$dockerExit}
Save-CreHandoffExclusive $receiptPath ($completion|ConvertTo-Json -Compress)
if($dockerExit -ne 0) {exit 1}
