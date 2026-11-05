# Offline: exercise the real Scheduler lookup with inert COM-shaped objects.
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreRestorationExecution.ps1')
. (Join-Path $PSScriptRoot 'CreRuntimeMaintenance.ps1')
$script:lookups=[Collections.Generic.List[string]]::new()
$script:currentTask='';$script:runningTask=''
$script:fixtureTask=[pscustomobject]@{}
$script:fixtureTask|Add-Member -MemberType ScriptMethod -Name GetInstances -Value {param($Flags); if($script:currentTask -ceq $script:runningTask){return ,@(1)}; return ,@()}
$script:fixtureFolder=[pscustomobject]@{}
$script:fixtureFolder|Add-Member -MemberType ScriptMethod -Name GetTask -Value {param($Name); $script:currentTask=$Name; $script:lookups.Add($Name); return $script:fixtureTask}
$script:fixtureService=[pscustomobject]@{}
$script:fixtureService|Add-Member -MemberType ScriptMethod -Name Connect -Value {}
$script:fixtureService|Add-Member -MemberType ScriptMethod -Name GetFolder -Value {param($Path); if($Path -cne '\Agentic Assets\CRE'){throw 'wrong folder'}; return $script:fixtureFolder}
function New-Object {param($ComObject); if($ComObject -cne 'Schedule.Service'){throw 'wrong service'}; return $script:fixtureService}
$rows=@(
 @{key='interra';task_path='\Agentic Assets\CRE\';task_name='Interra Refresh'},
 @{key='newmark';task_path='\Agentic Assets\CRE\';task_name='Newmark Refresh'},
 @{key='qualified';task_path='\Agentic Assets\CRE\';task_name='Qualified Brokerage Pipeline'},
 @{key='colliers-partial';task_path='\Agentic Assets\CRE\';task_name='Colliers Partial Refresh'},
 @{key='daum-subset';task_path='\Agentic Assets\CRE\';task_name='DAUM Verified Subset'},
 @{key='enrich:newmark';task_path='\Agentic Assets\CRE\';task_name='Enrichment - newmark'}
)
if(@(Get-CreMaintenanceTaskInstances @{task_baseline=$rows}).Count -ne 0 -or $script:lookups.Count -ne 6) {throw 'optional fleet lookup failed'}
$script:runningTask='DAUM Verified Subset'
$instances=@(Get-CreMaintenanceTaskInstances @{task_baseline=$rows})
if($instances.Count -ne 1 -or $instances[0].key -cne 'daum-subset') {throw 'active optional task was not detected'}
foreach($row in @(
 @{key='daum-subset';task_path='\Agentic Assets\CRE\';task_name='Newmark Refresh'},
 @{key='enrich:newmark';task_path='\Agentic Assets\CRE\';task_name='Enrichment - svn'},
 @{key='enrich:../bad';task_path='\Agentic Assets\CRE\';task_name='Enrichment - ../bad'},
 @{key='colliers-partial';task_path='\Agentic Assets\CRE\';task_name='Newmark Refresh'},
 @{key='colliers-partial';task_path='\Other\';task_name='Colliers Partial Refresh'},
 @{key='other';task_path='\Agentic Assets\CRE\';task_name='DAUM Verified Subset'},
 @{key='daum-subset';task_path='\Other\';task_name='DAUM Verified Subset'}
)) {
 $before=$script:lookups.Count;$rejected=$false
 try {$null=Get-CreRestorationRegisteredTask $script:fixtureService $row} catch {$rejected=$true}
 if(-not $rejected -or $script:lookups.Count -ne $before) {throw 'foreign lookup was allowed'}
}
Write-Output 'Optional task lookup fixture passed'
