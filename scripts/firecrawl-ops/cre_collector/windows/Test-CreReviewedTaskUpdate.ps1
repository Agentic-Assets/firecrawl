# Mocked task lifecycle fixture. Optional owner probe uses only a disposable WSL lock.
param([string]$OwnerRuntime='', [int]$OwnerPid=0, [string]$OwnerGeneration='', [string]$GitBarrier='', [string]$OldSha='', [string]$NewSha='')
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreReviewedTaskUpdate.ps1')
foreach($name in @('Assert-CreMaintenanceIdle','Get-CreMaintenanceTaskInstances','Get-CreRestorationRegisteredTask')) {
 if(-not (Get-Command $name -CommandType Function -ErrorAction SilentlyContinue)) {throw "Missing task dependency: $name"}
}
$realFleetCheck=${function:Assert-CreHandoffFleet}
function Assert-CreHandoffFleet($Release) {}
function Get-CreHandoffRelevantTasks($Release) {return @($script:rows|ForEach-Object { $_.task_path+$_.task_name })}
function Get-CreHandoffNativeFrontends {return @()}
function Get-CreMaintenanceTaskInstances($Release) {return @()}
Assert-CreMaintenanceIdle @{}
$old=if($OldSha){$OldSha}else{'a'*40};$new=if($NewSha){$NewSha}else{'b'*40}
$testRuntime=if($OwnerRuntime){$OwnerRuntime}else{'/home/cayman/code/firecrawl'}
$fixture=@{tasks=@{}; events=[Collections.Generic.List[string]]::new(); successor=$false; owner=$true}
$rows=@(
 @{key='interra';task_path='\Agentic Assets\CRE\';task_name='Interra Refresh'},
 @{key='newmark';task_path='\Agentic Assets\CRE\';task_name='Newmark Refresh'},
 @{key='qualified';task_path='\Agentic Assets\CRE\';task_name='Qualified Brokerage Pipeline'}
)
$fixtureSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$native=Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$exe=Join-Path $env:LOCALAPPDATA 'Programs/PowerShell/7/pwsh.exe'
$launcher=Join-Path $native 'scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1'
foreach($row in $rows) {
 $source=if($row.key -ceq 'interra'){'"-Source" "interra-realty"'}elseif($row.key -ceq 'newmark'){'"-Source" "newmark"'}else{'"-Sources" "interra-realty,newmark" "-Pipeline"'}
 $runtimeArg=if($OwnerRuntime){'"-LinuxRepo" "'+$OwnerRuntime+'" '}else{''}
 $argsText='"-NoProfile" "-NonInteractive" "-File" "'+$launcher+'" "-Tier" "series" '+$source+' '+$runtimeArg+'"-WorkDeadlineSeconds" "3600" "-AcknowledgeDatabaseWrites" "-ExpectedSha" "'+$old+'"'
 $xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><SecurityDescriptor>fixture-security</SecurityDescriptor><Description>Reviewed '+$old+'</Description></RegistrationInfo><Principals><Principal><UserId>'+$fixtureSid+'</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Actions><Exec><Command>'+[Security.SecurityElement]::Escape($exe)+'</Command><Arguments>'+[Security.SecurityElement]::Escape($argsText)+'</Arguments><WorkingDirectory>'+[Security.SecurityElement]::Escape($native)+'</WorkingDirectory></Exec></Actions><Settings><Enabled>true</Enabled></Settings><Triggers><CalendarTrigger><StartBoundary>2099-01-01T00:00:00</StartBoundary></CalendarTrigger></Triggers></Task>'
 $enabled=$row.key -cne 'newmark'
 $fixture.tasks[$row.key]=@{xml=(Set-CreHandoffEnabledXml $xml $enabled);enabled=$enabled}
}
function Assert-CreHandoffFleet($Release) {}
function Assert-CreMaintenanceIdle($Release) {if($fixture.successor){throw 'successor already running'}}
function Export-ScheduledTask($TaskPath,$TaskName,$ErrorAction) {
 $r=@($rows|Where-Object {$_.task_name -ceq $TaskName})[0];return $fixture.tasks[$r.key].xml
}
function Get-CreHandoffTask($Baseline,$Xml,$RequireNext,$IncludeMissed,$ExecutionSample) {
 $state=$fixture.tasks[$Baseline.key]
 if((Get-CreTaskContract $state.xml) -cne (Get-CreTaskContract $Xml)) {throw 'task contract changed'}
 return @{key=$Baseline.key;task_path=$Baseline.task_path;task_name=$Baseline.task_name;xml=$state.xml;enabled=$state.enabled;state=$(if($state.enabled){'Ready'}else{'Disabled'})}
}
function Disable-ScheduledTask($TaskPath,$TaskName,$ErrorAction) {
 $r=@($rows|Where-Object {$_.task_name -ceq $TaskName})[0];$fixture.events.Add('disable:'+ $r.key)
 $state=$fixture.tasks[$r.key];$state.enabled=$false;$state.xml=Set-CreHandoffEnabledXml $state.xml $false
}
function Enable-ScheduledTask($TaskPath,$TaskName,$ErrorAction) {
 $r=@($rows|Where-Object {$_.task_name -ceq $TaskName})[0];$fixture.events.Add('enable:'+ $r.key)
 $state=$fixture.tasks[$r.key];$state.enabled=$true;$state.xml=Set-CreHandoffEnabledXml $state.xml $true
 if($r.key -ceq 'interra') {$fixture.successor=$true}
}
function Update-CreReviewedTaskDefinition($Row,[string]$Xml) {
 $fixture.events.Add('pin:'+ $Row.key)
 $state=$fixture.tasks[$Row.key];$state.xml=$Xml;$state.enabled=$false
}
$assertOwner={if(-not $fixture.owner){throw 'owner lost'}}
$release=@{roots=@{native=$native;runtime=$testRuntime;development=$native};task_baseline=$rows;qualified_sources='interra-realty,newmark';supported_sources=@('interra-realty','newmark');wsl=@{distro='Ubuntu-24.04';user='cayman';env_file='/home/cayman/.config/cre/equire.env'}}
# Verify the native bridge checks the stable control bytes and passes the
# exact owner identity, without invoking a real WSL process in this fixture.
$control=Join-Path (Split-Path -Parent $PSScriptRoot) 'cre_reviewed_update.py'
$controlSha=Get-CreReviewedControlHash $control
$fixture.wslExit=0;$fixture.wslArgs=@()
function wsl.exe {$fixture.wslArgs=@($args);$global:LASTEXITCODE=$fixture.wslExit}
Assert-CreReviewedOwner $release $control $controlSha 4321 'fixture-generation'
if(($fixture.wslArgs -join '|') -cnotmatch ('--owner-check\|'+[regex]::Escape($testRuntime)+'\|4321\|fixture-generation$')) {throw 'Owner bridge arguments differ'}
$fixture.wslExit=1
try {Assert-CreReviewedOwner $release $control $controlSha 4321 'fixture-generation';throw 'owner loss accepted'} catch {if($_.Exception.Message -ceq 'owner loss accepted'){throw}}
$fixture.wslExit=0
try {Assert-CreReviewedOwner $release $control ('0'*64) 4321 'fixture-generation';throw 'changed control accepted'} catch {if($_.Exception.Message -ceq 'changed control accepted'){throw}}
Remove-Item Function:wsl.exe
if($OwnerRuntime) {
 $assertOwner={if(-not $fixture.owner){throw 'owner lost'};Assert-CreReviewedOwner $release $control $controlSha $OwnerPid $OwnerGeneration}
}
$wrong=@(@{key='interra';task_path='\Agentic Assets\CRE\';task_name='Wrong'},$rows[1],$rows[2])
try {Get-CreReviewedTaskSnapshot @{roots=$release.roots;task_baseline=$wrong} $old $new;throw 'foreign task accepted'} catch {if($_.Exception.Message -ceq 'foreign task accepted'){throw}}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('Invoke-CreGuardedTier.ps1','OtherScript.ps1')
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'foreign action accepted'} catch {if($_.Exception.Message -ceq 'foreign action accepted'){throw}}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('OtherScript.ps1','Invoke-CreGuardedTier.ps1')
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('"-ExpectedSha"','"-LinuxRepo" "/tmp/foreign" "-ExpectedSha"')
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'foreign WSL root accepted'} catch {if($_.Exception.Message -ceq 'foreign WSL root accepted'){throw}}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('"-LinuxRepo" "/tmp/foreign" "-ExpectedSha"','"-ExpectedSha"')
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('"-ExpectedSha"','"-Batch" "10" "-DeadlineSeconds" "45" "-ExpectedSha"')
$null=Get-CreReviewedTaskSnapshot $release $old $new
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('"-Batch" "10" "-DeadlineSeconds" "45" "-ExpectedSha"','"-ExpectedSha"')
$selectedRoot=Join-Path (Join-Path $native '.scratch') ('fixture-selected-'+[Guid]::NewGuid().ToString('N'))
$selectedDir=Join-Path $selectedRoot 'scripts/firecrawl-ops/cre_collector/windows'
$null=New-Item -ItemType Directory -Path $selectedDir -Force
$selectedLauncher=Join-Path $selectedDir 'Invoke-CreGuardedTier.ps1'
$selectedText=[IO.File]::ReadAllText($launcher)
[IO.File]::WriteAllText($selectedLauncher,$selectedText)
$release.roots.development=$selectedRoot
$null=Get-CreReviewedTaskSnapshot $release $old $new
[IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace('WorkDeadlineSeconds','RenamedWorkDeadline'))
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'incompatible selected launcher accepted'} catch {if($_.Exception.Message -ceq 'incompatible selected launcher accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Selected launcher incompatibility caused task mutation'}
if(-not $OwnerRuntime) {
 [IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace("'/home/cayman/code/firecrawl'","'/tmp/other-runtime'"))
 try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'changed selected default accepted'} catch {if($_.Exception.Message -ceq 'changed selected default accepted'){throw}}
 if($fixture.events.Count -ne 0) {throw 'Selected default drift caused task mutation'}
}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('"-ExpectedSha"','"-SourceWorkers" "3" "-ExpectedSha"')
$rangeOld='[ValidateRange(1, 4)][int]$SourceWorkers'
if(-not $selectedText.Contains($rangeOld)) {throw 'Fixture launcher range missing'}
[IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace($rangeOld,'[ValidateRange(1, 3)][int]$SourceWorkers'))
$null=Get-CreReviewedTaskSnapshot $release $old $new
[IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace($rangeOld,'[ValidateRange(1, 2)][int]$SourceWorkers'))
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'incompatible selected range accepted'} catch {if($_.Exception.Message -ceq 'incompatible selected range accepted'){throw};if($_.Exception.Message -cnotmatch 'SourceWorkers'){throw}}
[IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace($rangeOld,"[ValidateScript({throw 'validation body ran'})]"+$rangeOld))
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'changed custom validator accepted'} catch {if($_.Exception.Message -ceq 'changed custom validator accepted'){throw};if($_.Exception.Message -cnotmatch 'SourceWorkers.*requires review'){throw}}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('"-SourceWorkers" "3" "-ExpectedSha"','"-ExpectedSha"')
$tierOld="[ValidateSet('monitor', 'enrich', 'series')][string]"+'$Tier'
$tierNew="[ValidateSet('monitor', 'enrich')][string]"+'$Tier'
if(-not $selectedText.Contains($tierOld)) {throw 'Fixture launcher tier set missing'}
[IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace($tierOld,$tierNew))
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'incompatible selected set accepted'} catch {if($_.Exception.Message -ceq 'incompatible selected set accepted'){throw};if($_.Exception.Message -cnotmatch 'Tier'){throw}}
$switchOld='[switch]$Pipeline'
if(-not $selectedText.Contains($switchOld)) {throw 'Fixture launcher switch missing'}
[IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace($switchOld,("[ValidateScript({throw 'switch body ran'})]"+$switchOld)))
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'changed switch validator accepted'} catch {if($_.Exception.Message -ceq 'changed switch validator accepted'){throw};if($_.Exception.Message -cnotmatch 'Pipeline.*requires review'){throw}}
$pinOld="[ValidatePattern('^[0-9a-f]{40}$')][string]"+'$ExpectedSha'
if(-not $selectedText.Contains($pinOld)) {throw 'Fixture launcher pin declaration missing'}
$setDrift=$selectedText.Replace($switchOld,"[Parameter(ParameterSetName='A')]"+$switchOld).Replace($pinOld,"[Parameter(ParameterSetName='B')]"+$pinOld)
[IO.File]::WriteAllText($selectedLauncher,$setDrift)
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'incompatible parameter sets accepted'} catch {if($_.Exception.Message -ceq 'incompatible parameter sets accepted'){throw};if($_.Exception.Message -cnotmatch '(Pipeline|ExpectedSha).*binding requires review'){throw}}
$mandatory='[Parameter(Mandatory=$true)][string]$NewRequired,'
[IO.File]::WriteAllText($selectedLauncher,$selectedText.Replace("param(",("param(" + $mandatory)))
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'new mandatory option accepted'} catch {if($_.Exception.Message -ceq 'new mandatory option accepted'){throw};if($_.Exception.Message -cnotmatch 'NewRequired'){throw}}
if($fixture.events.Count -ne 0) {throw 'Selected binding mismatch caused task mutation'}
$release.roots.development=$native
foreach($bad in @('"-LinuxEnvFile" "/tmp/foreign"','"-CheckOnly"','"-Distro" "Foreign"','"-UnexpectedOption" "x"')) {
 $fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('"-ExpectedSha"',$bad+' "-ExpectedSha"')
 try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'unsafe task argument accepted'} catch {if($_.Exception.Message -ceq 'unsafe task argument accepted'){throw}}
 $fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace($bad+' "-ExpectedSha"','"-ExpectedSha"')
}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace($exe,'C:\Windows\System32\cmd.exe')
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'foreign executable accepted'} catch {if($_.Exception.Message -ceq 'foreign executable accepted'){throw}}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('C:\Windows\System32\cmd.exe',$exe)
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('<WorkingDirectory>'+[Security.SecurityElement]::Escape($native)+'</WorkingDirectory>','<WorkingDirectory>C:\other</WorkingDirectory>')
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'foreign working directory accepted'} catch {if($_.Exception.Message -ceq 'foreign working directory accepted'){throw}}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('<WorkingDirectory>C:\other</WorkingDirectory>','<WorkingDirectory>'+[Security.SecurityElement]::Escape($native)+'</WorkingDirectory>')
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace($fixtureSid,'S-1-5-18')
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'foreign principal accepted'} catch {if($_.Exception.Message -ceq 'foreign principal accepted'){throw}}
$fixture.tasks['interra'].xml=$fixture.tasks['interra'].xml.Replace('S-1-5-18',$fixtureSid)
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('interra-realty,newmark','jll,newmark')
try {$null=Get-CreReviewedTaskSnapshot $release $old $new;throw 'foreign source scope accepted'} catch {if($_.Exception.Message -ceq 'foreign source scope accepted'){throw}}
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('jll,newmark','interra-realty,newmark')
# A legitimate current trigger can differ from any historical packet.
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('2099-01-01','2099-03-04')
$release.Remove('qualified_sources')
$snapshot=Get-CreReviewedCurrentTaskSnapshot $release $old $new
$jsonRelease=Read-CreHandoffJson ($release|ConvertTo-Json -Depth 20)
$jsonSnapshot=Get-CreReviewedCurrentTaskSnapshot $jsonRelease $old $new
if(@($jsonSnapshot).Count -ne 3 -or $jsonRelease.qualified_sources -cne $release.qualified_sources) {throw 'Current JSON release failed'}
if((Get-CreReviewedSnapshotDigest $jsonRelease $jsonSnapshot $old $new) -cne (Get-CreReviewedSnapshotDigest $release $snapshot $old $new)) {throw 'Current JSON snapshot digest changed'}
if($OwnerRuntime) {
 $suffix=[Guid]::NewGuid().ToString('N')
 $stageCopy=Join-Path $PSScriptRoot ('fixture-stage-'+$suffix+'.ps1')
 $planFile=Join-Path $PSScriptRoot ('fixture-plan-'+$suffix+'.json')
 $resultFile=Join-Path $PSScriptRoot ('fixture-result-'+$suffix+'.json')
 $text=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-CreReviewedUpdate.ps1')).Replace(". (Join-Path $"+ "PSScriptRoot 'CreReviewedTaskUpdate.ps1')",'# already loaded')
 [IO.File]::WriteAllText($stageCopy,$text)
 [IO.File]::WriteAllText($planFile,(@{release=$release;old_sha=$old;new_sha=$new}|ConvertTo-Json -Depth 30 -Compress))
 $planSha=Get-CreReviewedControlHash $planFile
 & $stageCopy -Stage Inspect -Plan $planFile -PlanSha $planSha -Control $control -ControlSha $controlSha -OwnerPid $OwnerPid -Generation $OwnerGeneration -Output $resultFile -FailureOutput ($resultFile+'.failure')
 $result=Read-CreHandoffJson ([IO.File]::ReadAllText($resultFile))
 if(@($result.snapshot).Count -ne 3 -or $result.qualified_sources -cne $release.qualified_sources) {throw 'Native stage wrapper snapshot failed'}
}
$noopSnapshot=Get-CreReviewedCurrentTaskSnapshot $release $old $old
if(@($noopSnapshot).Count -ne 3 -or $fixture.events.Count -ne 0) {throw 'Current no-op snapshot failed or mutated tasks'}
if($release.qualified_sources -cne 'interra-realty,newmark') {throw 'current qualified sources not captured'}
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('interra-realty,newmark','newmark,interra-realty')
try {$null=Get-CreReviewedCurrentTaskSnapshot $release $old $new;throw 'changed qualified scope accepted'} catch {if($_.Exception.Message -ceq 'changed qualified scope accepted'){throw}}
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('newmark,interra-realty','interra-realty,newmark')
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('interra-realty,newmark','interra-realty,unknown-source')
$unbound=@{roots=$release.roots;task_baseline=$rows;supported_sources=@('interra-realty','newmark')}
try {$null=Get-CreReviewedCurrentTaskSnapshot $unbound $old $new;throw 'unknown qualified source accepted'} catch {if($_.Exception.Message -ceq 'unknown qualified source accepted'){throw}}
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('interra-realty,unknown-source','interra-realty,newmark')
if(@($snapshot).Count -ne 3 -or $snapshot[1].enabled -ne $false) {throw 'snapshot did not preserve task flags'}
$digest=Get-CreReviewedSnapshotDigest $release $snapshot $old $new
if($OwnerRuntime) {
 $applyPlan=Join-Path $PSScriptRoot ('fixture-apply-'+$suffix+'.json')
 [IO.File]::WriteAllText($applyPlan,(@{release=$release;old_sha=$old;new_sha=$new;snapshot=$snapshot;digest=$digest}|ConvertTo-Json -Depth 30 -Compress))
 $round=Read-CreHandoffJson ([IO.File]::ReadAllText($applyPlan))
 for($i=0;$i -lt $snapshot.Count;$i++) {
  if((Get-CreHandoffHash ([string]$snapshot[$i].xml)) -cne (Get-CreHandoffHash ([string]$round.snapshot[$i].xml))) {throw 'Snapshot XML changed in JSON'}
  if($snapshot[$i].enabled -ne $round.snapshot[$i].enabled) {throw 'Snapshot flag changed in JSON'}
  foreach($name in @('key','task_path','task_name')) {if([string]$snapshot[$i].$name -cne [string]$round.snapshot[$i].$name){throw "Snapshot $name changed in JSON"}}
 }
 foreach($name in @('distro','user','env_file')) {if([string]$release.wsl.$name -cne [string]$round.release.wsl.$name){throw "WSL $name changed in JSON"}}
 if([string]$release.qualified_sources -cne [string]$round.release.qualified_sources){throw 'Qualified scope changed in JSON'}
 if(($release.supported_sources -join ',') -cne ($round.release.supported_sources -join ',')){throw 'Registry changed in JSON'}
 foreach($name in @('native','runtime')) {if([string]$release.roots.$name -cne [string]$round.release.roots.$name){throw 'Root changed in JSON'}}
 if((Get-CreReviewedSnapshotDigest $round.release @($round.snapshot) $old $new) -cne $digest) {throw ('Snapshot digest changed in JSON: original order '+(@($snapshot|Sort-Object key|ForEach-Object {$_.key}) -join ',')+'; decoded order '+(@($round.snapshot|Sort-Object key|ForEach-Object {$_.key}) -join ','))}
 $applySha=Get-CreReviewedControlHash $applyPlan
 function Invoke-FixtureStage([string]$stage) {
  $out=Join-Path $PSScriptRoot ('fixture-result-'+$suffix+'-'+$stage+'.json')
  & $stageCopy -Stage $stage -Plan $applyPlan -PlanSha $applySha -Control $control -ControlSha $controlSha -OwnerPid $OwnerPid -Generation $OwnerGeneration -Output $out -FailureOutput ($out+'.failure')
  return (Read-CreHandoffJson ([IO.File]::ReadAllText($out)))
 }
 try {Invoke-FixtureStage 'VerifyHeld';throw 'unheld fleet accepted'} catch {if($_.Exception.Message -ceq 'unheld fleet accepted'){throw}}
 $failurePath=Join-Path $PSScriptRoot ('fixture-result-'+$suffix+'-VerifyHeld.json.failure')
 $receipt=Read-CreHandoffJson ([IO.File]::ReadAllText($failurePath))
 if($receipt.stage -cne 'VerifyHeld' -or $receipt.plan_sha256 -cne $applySha -or $receipt.owner_pid -ne $OwnerPid -or
    $receipt.generation_sha256 -cne (Get-CreHandoffHash $OwnerGeneration) -or $receipt.completed -ne $true) {throw 'Native failure receipt binding differs'}
 $suffix=[Guid]::NewGuid().ToString('N')
}
$altered=@($snapshot|ForEach-Object {[ordered]@{key=$_.key;task_path=$_.task_path;task_name=$_.task_name;xml=$_.xml;enabled=$_.enabled}})
$altered[2].xml=$altered[2].xml.Replace('2099-03-04','2099-04-05')
try {Invoke-CreReviewedTaskHold $release $altered $old $new $digest $assertOwner;throw 'changed snapshot accepted'} catch {if($_.Exception.Message -ceq 'changed snapshot accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Changed snapshot caused mutation'}
$release.supported_sources=@('interra-realty','newmark','jll')
try {Invoke-CreReviewedTaskHold $release $snapshot $old $new $digest $assertOwner;throw 'changed source registry accepted'} catch {if($_.Exception.Message -ceq 'changed source registry accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Changed registry caused mutation'}
$release.supported_sources=@('interra-realty','newmark')
$fixture.tasks['qualified'].xml=$snapshot[2].xml.Replace('fixture-security','foreign-security')
try {Invoke-CreReviewedTaskHold $release $snapshot $old $new $digest $assertOwner;throw 'security descriptor drift accepted'} catch {if($_.Exception.Message -ceq 'security descriptor drift accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Security descriptor drift caused mutation'}
$fixture.tasks['qualified'].xml=$snapshot[2].xml
$fixture.tasks['qualified'].xml=$snapshot[2].xml.Replace('2099-03-04','2099-03-05')
try {Invoke-CreReviewedTaskHold $release $snapshot $old $new $digest $assertOwner;throw 'drift accepted'} catch {if($_.Exception.Message -ceq 'drift accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Known fleet drift caused a partial hold'}
$fixture.tasks['qualified'].xml=$snapshot[2].xml
$fixture.owner=$false
try {Invoke-CreReviewedTaskHold $release $snapshot $old $new $digest $assertOwner;throw 'owner loss accepted'} catch {if($_.Exception.Message -ceq 'owner loss accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Owner loss caused a mutation'}
$fixture.owner=$true
# Reconcile a partially completed hold from current readbacks only.
$fixture.tasks['interra'].xml=Set-CreHandoffEnabledXml $snapshot[0].xml $false
$fixture.tasks['interra'].enabled=$false
$fixture.events.Clear();$fixture.successor=$false
try {Invoke-CreReviewedTaskReconcileHold $release $snapshot $old $new $digest {throw 'old code differs'};throw 'changed code accepted'} catch {if($_.Exception.Message -ceq 'changed code accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Changed code caused reconciliation'}
$fixture.tasks['qualified'].xml=$snapshot[2].xml.Replace($old,$new)
try {Invoke-CreReviewedTaskReconcileHold $release $snapshot $old $new $digest $assertOwner;throw 'changed pin accepted'} catch {if($_.Exception.Message -ceq 'changed pin accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Changed pin caused reconciliation'}
$fixture.tasks['qualified'].xml=$snapshot[2].xml
Invoke-CreReviewedTaskReconcileHold $release $snapshot $old $new $digest $assertOwner
if(-not $fixture.tasks['interra'].enabled -or -not $fixture.successor -or $fixture.tasks['newmark'].enabled) {throw 'Partial hold reconciliation lost task state'}
if(@($fixture.events|Where-Object {$_ -like 'disable:*'}).Count -ne 0) {throw 'Reconciliation disabled a successor'}
$fixture.successor=$false;$fixture.events.Clear()
$holdResult=if($OwnerRuntime){Invoke-FixtureStage 'Hold'}else{Invoke-CreReviewedTaskStage 'Hold' $release $snapshot $old $new $digest $assertOwner}
if(-not $holdResult.complete) {throw 'Hold stage did not complete'}
$verifyResult=if($OwnerRuntime){Invoke-FixtureStage 'VerifyHeld'}else{Invoke-CreReviewedTaskStage 'VerifyHeld' $release $snapshot $old $new $digest $assertOwner}
if(-not $verifyResult.complete) {throw 'Held verification did not complete'}
if(@($fixture.events|Where-Object {$_ -ceq 'disable:newmark'}).Count) {throw 'disabled task was altered'}
if($GitBarrier) {
 [IO.File]::WriteAllText($GitBarrier+'.held','held')
 $deadline=[DateTime]::UtcNow.AddSeconds(60)
 while(-not [IO.File]::Exists($GitBarrier+'.continue')) {
  if([DateTime]::UtcNow -ge $deadline) {throw 'Git fixture timed out'}
  & $assertOwner
  Start-Sleep -Milliseconds 100
 }
}
$repinResult=if($OwnerRuntime){Invoke-FixtureStage 'Repin'}else{Invoke-CreReviewedTaskStage 'Repin' $release $snapshot $old $new $digest $assertOwner}
$restoreResult=if($OwnerRuntime){Invoke-FixtureStage 'Restore'}else{Invoke-CreReviewedTaskStage 'Restore' $release $snapshot $old $new $digest $assertOwner}
$restoredResult=if($OwnerRuntime){Invoke-FixtureStage 'VerifyRestored'}else{Invoke-CreReviewedTaskStage 'VerifyRestored' $release $snapshot $old $new $digest $assertOwner}
if(-not $repinResult.complete -or -not $restoreResult.complete -or -not $restoredResult.complete) {throw 'Restore stage did not complete'}
if(-not $fixture.successor) {throw 'successor simulation did not start'}
if($fixture.tasks['newmark'].enabled) {throw 'original disabled flag changed'}
if(-not $fixture.tasks['qualified'].enabled) {throw 'later task was not restored'}
if(@($fixture.events|Where-Object {$_ -like 'disable:*'}).Count -ne 2) {throw 'restore disabled a successor'}
# Owner loss after the first enable must not disable the now-running successor.
foreach($row in $snapshot) {
 $fixture.tasks[$row.key].xml=Set-CreHandoffEnabledXml (Pin-ReviewedXml $row.xml $old $new) $false
 $fixture.tasks[$row.key].enabled=$false
}
$fixture.successor=$false;$fixture.events.Clear();$fixture.ownerChecks=0
$lostOwner={ $fixture.ownerChecks++; if($fixture.ownerChecks -ge 3){throw 'owner lost during restore'} }
try {Invoke-CreReviewedTaskRestore $release $snapshot $old $new $digest $lostOwner;throw 'owner loss accepted'} catch {if($_.Exception.Message -ceq 'owner loss accepted'){throw}}
if(-not $fixture.tasks['interra'].enabled -or -not $fixture.successor) {throw 'successor was not preserved after owner loss'}
if(@($fixture.events|Where-Object {$_ -like 'disable:*'}).Count -ne 0) {throw 'owner-loss path disabled successor'}
$mutated=Set-CreHandoffEnabledXml $snapshot[0].xml $true
$fixture.tasks['interra'].xml=$mutated.Replace('2099-01-01','2099-01-02')
try { Assert-CreReviewedTaskContract $snapshot[0] $fixture.tasks['interra'].xml $true; throw 'drift accepted' } catch {if($_.Exception.Message -ceq 'drift accepted'){throw}}
# A positively completed partial repin can finish only captured old/selected definitions.
foreach($row in $snapshot) {
 $xml=if($row.key -ceq 'interra'){Pin-ReviewedXml ([string]$row.xml) $old $new}else{[string]$row.xml}
 $fixture.tasks[$row.key].xml=Set-CreHandoffEnabledXml $xml $false
 $fixture.tasks[$row.key].enabled=$false
}
$fixture.successor=$false;$fixture.events.Clear()
$fixture.tasks['qualified'].xml=$fixture.tasks['qualified'].xml.Replace('2099-03-04','2099-03-06')
try {Complete-CreReviewedRepin $release $snapshot $old $new $digest $assertOwner;throw 'drifted mixed fleet accepted'} catch {if($_.Exception.Message -ceq 'drifted mixed fleet accepted'){throw}}
if($fixture.events.Count -ne 0) {throw 'Mixed-fleet drift caused mutation'}
$fixture.tasks['qualified'].xml=Set-CreHandoffEnabledXml ([string]$snapshot[2].xml) $false
Complete-CreReviewedRepin $release $snapshot $old $new $digest $assertOwner
foreach($row in $snapshot) {if((Get-CreReviewedSettledTask $row $old $new $false).pin -cne 'selected'){throw 'Partial repin not completed'}}
# A previous restore may already have started a successor: never disable it.
$fixture.tasks['interra'].enabled=$true
$fixture.tasks['interra'].xml=Set-CreHandoffEnabledXml (Pin-ReviewedXml ([string]$snapshot[0].xml) $old $new) $true
$fixture.successor=$true;$fixture.events.Clear()
Complete-CreReviewedRestore $release $snapshot $old $new $digest $assertOwner
if(-not $fixture.tasks['interra'].enabled -or $fixture.tasks['newmark'].enabled -or -not $fixture.tasks['qualified'].enabled) {throw 'Partial restore lost flags'}
if(@($fixture.events|Where-Object {$_ -like 'disable:*'}).Count) {throw 'Partial restore disabled a successor'}
# Runtime may already be at selected while the native checkout advances.
foreach($row in $snapshot) {
 $fixture.tasks[$row.key].xml=Set-CreHandoffEnabledXml ([string]$row.xml) $false
 $fixture.tasks[$row.key].enabled=$false
}
$fixture.successor=$false;$fixture.events.Clear()
$noopDigest=Get-CreReviewedSnapshotDigest $release $snapshot $old $old
Invoke-CreReviewedTaskRestore $release $snapshot $old $old $noopDigest $assertOwner
if(-not $fixture.tasks['interra'].enabled -or $fixture.tasks['newmark'].enabled -or -not $fixture.tasks['qualified'].enabled) {throw 'Same-pin restore lost flags'}
if(@($fixture.events|Where-Object {$_ -like 'pin:*'}).Count) {throw 'Same-pin restore repinned a task'}
$fixture.securityCalls=0;$fixture.securityMismatch=$false;$fixture.securityUnavailable=$false
$registered=[pscustomobject]@{}
$registered | Add-Member ScriptMethod GetSecurityDescriptor {param($Flags) if($Flags -ne 7){throw 'Wrong security scope'};$fixture.securityCalls++;if($fixture.securityUnavailable){throw 'synthetic unavailable'};if($fixture.securityMismatch -and $fixture.securityCalls -gt 1){return 'different'};return 'same'}
$folder=[pscustomobject]@{registered=$registered}
$folder | Add-Member ScriptMethod GetTask {param($Name) if($Name -cne 'Interra Refresh'){throw 'Wrong task'};return $this.registered}
$folder | Add-Member ScriptMethod RegisterTask {param($Name,$Xml,$Flags,$Sid,$Password,$Logon,$Sddl) $fixture.comArgs=@($Name,$Xml,$Flags,$Sid,$Password,$Logon,$Sddl);return $null}
$service=[pscustomobject]@{folder=$folder}
$service | Add-Member ScriptMethod GetFolder {param($Path) if($Path -cne '\Agentic Assets\CRE'){throw 'Wrong COM folder'};return $this.folder}
Invoke-CreReviewedTaskDefinitionUpdate $service $rows[0] '<task/>' $fixtureSid
if($fixture.comArgs[0] -cne 'Interra Refresh' -or $fixture.comArgs[2] -ne 52 -or $fixture.comArgs[3] -cne $fixtureSid -or $null -ne $fixture.comArgs[4] -or $fixture.comArgs[5] -ne 3 -or $null -ne $fixture.comArgs[6]) {throw 'Task update must preserve ACL and suppress registration triggers'}
if($fixture.securityCalls -ne 2) {throw 'Expected both current ACL observations'}
$fixture.securityCalls=0;$fixture.securityMismatch=$true
try {Invoke-CreReviewedTaskDefinitionUpdate $service $rows[0] '<task/>' $fixtureSid;throw 'changed task security accepted'} catch {if($_.Exception.Message -ceq 'changed task security accepted'){throw}}
$fixture.securityCalls=0;$fixture.securityMismatch=$false;$fixture.securityUnavailable=$true
Invoke-CreReviewedTaskDefinitionUpdate $service $rows[0] '<task/>' $fixtureSid
# Optional source-bound task: use only fake definitions and scheduler functions.
foreach($row in $snapshot) {$fixture.tasks[$row.key].xml=[string]$row.xml;$fixture.tasks[$row.key].enabled=$row.enabled}
$fixture.successor=$false;$fixture.events.Clear()
$daum=@{key='daum-subset';task_path='\Agentic Assets\CRE\';task_name='DAUM Verified Subset'}
$rows=@($rows)+@($daum)
$daumXml=Set-CreHandoffEnabledXml (([string]$snapshot[1].xml).Replace('"-Source" "newmark"','"-Source" "daum-commercial" "-AdmitDaumVerifiedSubset"')) $true
$fixture.tasks[$daum.key]=@{xml=$daumXml;enabled=$true}
$daumRelease=@{roots=$release.roots;task_baseline=@($rows[0..2]);qualified_sources='interra-realty,newmark';supported_sources=@('interra-realty','newmark','daum-commercial');wsl=$release.wsl}
$daumSnapshot=@(Get-CreReviewedCurrentTaskSnapshot $daumRelease $old $new)
if($daumSnapshot.Count -ne 4 -or $daumSnapshot[3].key -cne 'daum-subset') {throw 'DAUM task discovery failed'}
$fixture.tasks[$daum.key].xml=$daumXml.Replace('"-AdmitDaumVerifiedSubset"','"-AdmitDaumVerifiedSubset" "-DaumSubsetPolicy" "tenure-and-empty-title-v2" "-AttemptsPerSource" "1"')
Assert-CreReviewedAction $daumRelease $daum $fixture.tasks[$daum.key].xml $old
 $fixture.tasks[$daum.key].xml=$fixture.tasks[$daum.key].xml.Replace('"-AttemptsPerSource" "1"','"-AttemptsPerSource" "0"')
try {Assert-CreReviewedAction $daumRelease $daum $fixture.tasks[$daum.key].xml $old;throw 'invalid DAUM attempt count accepted'} catch {if($_.Exception.Message -ceq 'invalid DAUM attempt count accepted'){throw}}
foreach($invalidPolicy in @('unknown','tenure-and-empty-title-v2" "-DaumSubsetPolicy" "tenure-only-v1')) {
 $fixture.tasks[$daum.key].xml=$daumXml.Replace('"-AdmitDaumVerifiedSubset"','"-AdmitDaumVerifiedSubset" "-DaumSubsetPolicy" "'+$invalidPolicy+'"')
 try {$null=Get-CreReviewedTaskSnapshot $daumRelease $old $new;throw 'invalid DAUM policy accepted'} catch {if($_.Exception.Message -ceq 'invalid DAUM policy accepted'){throw}}
}
foreach($mutation in @(
  @('"-AdmitDaumVerifiedSubset"',''),
  @('"-AdmitDaumVerifiedSubset"','"-AdmitDaumVerifiedSubset" "-AdmitDaumVerifiedSubset"'),
  @('"-AcknowledgeDatabaseWrites"',''),
  @('"-Source" "daum-commercial"','"-Source" "newmark"'),
  @('"-AdmitDaumVerifiedSubset"','"-AdmitDaumVerifiedSubset" "-Pipeline"'),
  @('"-AdmitDaumVerifiedSubset"','"-AdmitDaumVerifiedSubset" "-AdmitCurrentInventorySource" "daum-commercial"')
)) {
 $fixture.tasks[$daum.key].xml=$daumXml.Replace($mutation[0],$mutation[1])
 try {$null=Get-CreReviewedTaskSnapshot $daumRelease $old $new;throw 'invalid DAUM task accepted'} catch {if($_.Exception.Message -ceq 'invalid DAUM task accepted'){throw}}
}
$fixture.tasks[$daum.key].xml=$daumXml
$fixture.tasks['newmark'].xml=([string]$snapshot[1].xml).Replace('"-Source" "newmark"','"-Source" "newmark" "-AdmitDaumVerifiedSubset"')
try {$null=Get-CreReviewedTaskSnapshot $daumRelease $old $new;throw 'DAUM admission on another task accepted'} catch {if($_.Exception.Message -ceq 'DAUM admission on another task accepted'){throw}}
$fixture.tasks['newmark'].xml=([string]$snapshot[1].xml).Replace('"-Source" "newmark"','"-Source" "newmark" "-DaumSubsetPolicy" "tenure-and-empty-title-v2"')
try {$null=Get-CreReviewedTaskSnapshot $daumRelease $old $new;throw 'DAUM policy on another task accepted'} catch {if($_.Exception.Message -ceq 'DAUM policy on another task accepted'){throw}}
$fixture.tasks['newmark'].xml=[string]$snapshot[1].xml
$fixture.tasks[$daum.key].xml=$daumXml.Replace('"-AdmitDaumVerifiedSubset"','"-AdmitDaumVerifiedSubset" "-DaumSubsetPolicy" "tenure-and-empty-title-v2" "-AttemptsPerSource" "1"')
$daumSnapshot=@(Get-CreReviewedTaskSnapshot $daumRelease $old $new)
$daumDigest=Get-CreReviewedSnapshotDigest $daumRelease $daumSnapshot $old $new
Invoke-CreReviewedTaskHold $daumRelease $daumSnapshot $old $new $daumDigest $assertOwner
Invoke-CreReviewedTaskRepin $daumRelease $daumSnapshot $old $new $daumDigest $assertOwner
if($fixture.tasks[$daum.key].xml -cnotmatch $new) {throw 'DAUM task not repinned'}
if($fixture.tasks[$daum.key].xml -cnotmatch 'tenure-and-empty-title-v2') {throw 'DAUM subset policy lost during repin'}
if($fixture.tasks[$daum.key].xml -cnotmatch 'AttemptsPerSource') {throw 'DAUM attempt count lost during repin'}
Invoke-CreReviewedTaskRestore $daumRelease $daumSnapshot $old $new $daumDigest $assertOwner
if(-not $fixture.tasks[$daum.key].enabled) {throw 'DAUM task enabled state not restored'}
$fixture.tasks.Remove($daum.key);$rows=@($rows[0..2])
foreach($row in $snapshot) {$fixture.tasks[$row.key].xml=[string]$row.xml;$fixture.tasks[$row.key].enabled=$row.enabled}
$fixture.successor=$false;$fixture.events.Clear()
$extra=@{key='enrich:newmark';task_path='\Agentic Assets\CRE\';task_name='Enrichment - newmark'}
$rows=@($rows)+@($extra)
$base=[string]$snapshot[1].xml
$base=$base.Replace('"-Tier" "series"','"-Tier" "enrich"').Replace('"-WorkDeadlineSeconds" "3600" "-AcknowledgeDatabaseWrites"','"-Batch" "12" "-DeadlineSeconds" "300" "-Apply"')
foreach($applied in @($false,$true)) {
foreach($enabled in @($false,$true)) {
 if($applied) {$actionXml=$base} else {$actionXml=$base.Replace('"-Apply"','')}
 $fixture.tasks[$extra.key]=@{xml=(Set-CreHandoffEnabledXml $actionXml $enabled);enabled=$enabled}
 $optionalRelease=@{roots=$release.roots;task_baseline=@($rows[0..2]);qualified_sources='interra-realty,newmark';supported_sources=@('interra-realty','newmark');wsl=$release.wsl}
 $optional=@(Get-CreReviewedCurrentTaskSnapshot $optionalRelease $old $new)
 if($optional.Count -ne 4 -or $optional[3].enabled -ne $enabled) {throw 'Optional task discovery or flag failed'}
 & $realFleetCheck $optionalRelease
 $fixture.tasks[$extra.key].xml=([string]$optional[3].xml).Replace('"-Apply"','')
 $null=Get-CreReviewedTaskSnapshot $optionalRelease $old $new
 $fixture.tasks[$extra.key].xml=[string]$optional[3].xml
 foreach($mutation in @(
   @('"-Batch" "12"','"-Batch" "0"'),
   @('"-Source" "newmark"','"-Source" "interra-realty"')
 )) {
   $fixture.tasks[$extra.key].xml=([string]$optional[3].xml).Replace($mutation[0],$mutation[1])
   try {$null=Get-CreReviewedTaskSnapshot $optionalRelease $old $new;throw 'invalid enrichment accepted'} catch {if($_.Exception.Message -ceq 'invalid enrichment accepted'){throw}}
 }
 $fixture.tasks[$extra.key].xml=[string]$optional[3].xml
 $d=Get-CreReviewedSnapshotDigest $optionalRelease $optional $old $new
 $fixture.tasks[$extra.key].xml=$fixture.tasks[$extra.key].xml.Replace('newmark','other')
 try {Invoke-CreReviewedTaskHold $optionalRelease $optional $old $new $d $assertOwner;throw 'optional action drift accepted'} catch {if($_.Exception.Message -ceq 'optional action drift accepted'){throw}}
 $fixture.tasks[$extra.key].xml=[string]$optional[3].xml
 Invoke-CreReviewedTaskHold $optionalRelease $optional $old $new $d $assertOwner
 Invoke-CreReviewedTaskRepin $optionalRelease $optional $old $new $d $assertOwner
 if($fixture.tasks[$extra.key].xml -cnotmatch $new) {throw 'Optional task not repinned'}
 Invoke-CreReviewedTaskRestore $optionalRelease $optional $old $new $d $assertOwner
 if($fixture.tasks[$extra.key].enabled -ne $enabled) {throw 'Optional enabled flag not restored'}
 foreach($row in $snapshot) {$fixture.tasks[$row.key].xml=[string]$row.xml;$fixture.tasks[$row.key].enabled=$row.enabled}
 $fixture.successor=$false;$fixture.events.Clear()
}
}
# Both optional tasks participate in one fleet and retain their own enabled flags.
$rows=@($rows[0..2])+@($daum,$extra)
$fixture.tasks[$daum.key]=@{xml=$daumXml;enabled=$true}
$fixture.tasks[$extra.key]=@{xml=(Set-CreHandoffEnabledXml $base $false);enabled=$false}
$combinedRelease=@{roots=$release.roots;task_baseline=@($rows[0..2]);qualified_sources='interra-realty,newmark';supported_sources=@('interra-realty','newmark','daum-commercial');wsl=$release.wsl}
$combined=@(Get-CreReviewedCurrentTaskSnapshot $combinedRelease $old $new)
if($combined.Count -ne 5 -or @($combined | Where-Object {$_.key -ceq 'daum-subset'}).Count -ne 1 -or
   @($combined | Where-Object {$_.key -ceq 'enrich:newmark'}).Count -ne 1) {throw 'Combined optional fleet discovery failed'}
& $realFleetCheck $combinedRelease
$combinedDigest=Get-CreReviewedSnapshotDigest $combinedRelease $combined $old $new
Invoke-CreReviewedTaskHold $combinedRelease $combined $old $new $combinedDigest $assertOwner
Invoke-CreReviewedTaskRepin $combinedRelease $combined $old $new $combinedDigest $assertOwner
Invoke-CreReviewedTaskRestore $combinedRelease $combined $old $new $combinedDigest $assertOwner
foreach($row in $combined) {
 $actual=$fixture.tasks[$row.key]
 $expected=Set-CreHandoffEnabledXml (Pin-ReviewedXml ([string]$row.xml) $old $new) $row.enabled
 if($actual.enabled -ne $row.enabled -or (Get-CreTaskContract ([string]$actual.xml)) -cne (Get-CreTaskContract $expected)) {throw 'Combined fleet changed beyond pin or enabled state'}
}
$fixture.tasks.Remove($daum.key)
foreach($row in $snapshot) {$fixture.tasks[$row.key].xml=[string]$row.xml;$fixture.tasks[$row.key].enabled=$row.enabled}
$fixture.successor=$false;$fixture.events.Clear()
$matthews=@{key='enrich:matthews';task_path='\Agentic Assets\CRE\';task_name='Enrichment - matthews'}
$rows=@($rows[0..2])+@($matthews)
$matthewsXml=$base.Replace('newmark','matthews').Replace('"-DeadlineSeconds" "300"','"-DeadlineSeconds" "900"')
$fixture.tasks[$matthews.key]=@{xml=(Set-CreHandoffEnabledXml $matthewsXml.Replace('"-Apply"','') $false);enabled=$false}
$matthewsRelease=@{roots=$release.roots;task_baseline=@($rows[0..2]);qualified_sources='interra-realty,newmark';supported_sources=@('interra-realty','newmark','matthews');wsl=$release.wsl}
$null=Get-CreReviewedCurrentTaskSnapshot $matthewsRelease $old $new
$fixture.tasks[$matthews.key].xml=Set-CreHandoffEnabledXml $matthewsXml $false
try {$null=Get-CreReviewedTaskSnapshot $matthewsRelease $old $new;throw 'applied Matthews deadline accepted'} catch {if($_.Exception.Message -ceq 'applied Matthews deadline accepted'){throw}}
$rows=@($rows[0..2])+@($extra)
$bad=$rows+@(@{key='unknown';task_path='\Agentic Assets\CRE\';task_name='Other'})
$rows=$bad
try {& $realFleetCheck $optionalRelease;throw 'fleet drift accepted'} catch {if($_.Exception.Message -ceq 'fleet drift accepted'){throw}}
Set-Item Function:Assert-CreHandoffFleet $realFleetCheck
foreach($stage in @('Hold','VerifyHeld','Repin','Restore','VerifyRestored','ReconcileHold','VerifyOriginal','CompleteRepin','CompleteRestore')) {
 try {Invoke-CreReviewedTaskStage $stage $optionalRelease $optional $old $new $d $assertOwner;throw 'stage accepted fleet drift'} catch {if($_.Exception.Message -cnotmatch 'Whole CRE fleet|Unexpected or missing CRE task') {throw}}
}
try {$unknownRelease=@{task_baseline=@($bad[0..2]);supported_sources=@('newmark');roots=$release.roots};Add-CreReviewedOptionalTasks $unknownRelease;& $realFleetCheck $unknownRelease;throw 'unknown task accepted'} catch {if($_.Exception.Message -ceq 'unknown task accepted'){throw}}
Write-Output 'CRE reviewed task lifecycle fixture passed'
