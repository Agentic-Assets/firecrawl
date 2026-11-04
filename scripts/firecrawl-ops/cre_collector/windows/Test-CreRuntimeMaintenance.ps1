# Offline native maintenance fixture. Scheduler, COM, admission and history are mocked.
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
if(-not (Get-Command Invoke-CreRuntimeMaintenanceTasks -ErrorAction SilentlyContinue)) {
    throw 'Native maintenance task consumer is missing'
}
$root=Join-Path ([IO.Path]::GetTempPath()) ('cre-maintenance-fixture-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
$fixture=@{fleet=@{};registers=0;enables=0;owners=0;instances=@();root=$root}
function Check($Value,[string]$Message) {if(-not $Value){throw $Message}}
function Refuses([scriptblock]$Action,[string]$Expected) {
    $caught=$null;try{& $Action|Out-Null}catch{$caught=$_}
    Check ($null -ne $caught -and $caught.Exception.Message.Contains($Expected)) ('Expected refusal: '+$Expected)
}
try {
    $old='a'*40;$new='b'*40
    $xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>Reviewed '+$old+'</Description></RegistrationInfo><Principals><Principal><UserId>fixture</UserId></Principal></Principals><Actions><Exec><Command>pwsh.exe</Command><Arguments>-ExpectedSha '+$old+'</Arguments><WorkingDirectory>C:/runtime</WorkingDirectory></Exec></Actions><Settings><Enabled>false</Enabled></Settings></Task>'
    $last='2026-01-01T00:00:00Z';$next='2099-01-01T00:00:00Z'
    $rows=@();$inputs=@()
    foreach($pair in @(@('interra','Interra Refresh'),@('newmark','Newmark Refresh'),@('qualified','Qualified Brokerage Pipeline'))) {
        $key=$pair[0];$name=$pair[1];$path=Join-Path $root ($key+'.xml')
        [IO.File]::WriteAllText($path,$xml,[Text.UTF8Encoding]::new($false))
        $inputs+=@{role=$key;path=$path;sha256=(Get-CreHandoffHash $xml);bytes=([Text.UTF8Encoding]::new($false).GetByteCount($xml))}
        $rows+=@{key=$key;task_path='\Agentic Assets\CRE\';task_name=$name;xml=$key;enabled=$false;missed_runs=4;last_task_result=1;last_run_utc=$last;next_run_utc=$next}
        $fixture.fleet[$name]=@{xml=$xml;enabled=$false;state='Disabled'}
    }
    $sourceRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..')).Replace('\','/')
    $release=[pscustomobject]@{schema='cre-runtime-maintenance-v1';old=@{sha=$old};new=@{sha=$new};native_before=@{sha=$new};roots=@{native='C:/runtime';runtime='/home/fixture/runtime';tooling=$sourceRoot};inputs=$inputs;task_baseline=$rows}
    $wire=$release|ConvertTo-Json -Depth 12
    $packetPath=Join-Path $root 'maintenance.json'
    [IO.File]::WriteAllText($packetPath,$wire,[Text.UTF8Encoding]::new($false))
    $frontdoor=Join-Path $PSScriptRoot 'Invoke-CreCodeHandoff.ps1'
    $preview=Read-CreHandoffJson ((& $frontdoor -Mode Preview -MaintenancePath $packetPath -MaintenanceSha (Get-CreHandoffHash $wire))|Out-String)
    Check ($preview.preview_only -and -not $preview.execution -and $preview.selected.sha -ceq $new) 'Maintenance frontdoor must preview the selected build without host calls'
    function Assert-CreHandoffFleet {param($Release);Check ($fixture.fleet.Count -eq 3) 'Wrong fleet'}
    function Get-CreHandoffNativeFrontends {return @()}
    function Get-CreMaintenanceTaskInstances {param($Release);return $fixture.instances}
    function Get-CreHandoffAdmission {param($Release,$Output,$Stage);return @{admitted=$true}}
    function Save-CreRestorationWatermark {param($Output);Save-CreHandoffExclusive (Join-Path $Output 'scheduler-watermark.json') '{"fixture":true}'}
    function Get-CreRestorationExecution {param($Release,$Origin,$Output,$Tasks);return @{fixture=$true;origin=$Origin}}
    function Get-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);$t=$fixture.fleet[$TaskName];return [pscustomobject]@{TaskPath=$TaskPath;TaskName=$TaskName;State=$t.state;Settings=[pscustomobject]@{Enabled=$t.enabled};Actions=@([pscustomobject]@{Execute='pwsh.exe';Arguments='fixture';WorkingDirectory='C:/runtime'})}}
    function Get-ScheduledTaskInfo {param($TaskPath,$TaskName,$ErrorAction);return [pscustomobject]@{LastRunTime=[DateTimeOffset]::Parse($last).UtcDateTime;NextRunTime=[DateTimeOffset]::Parse($next).UtcDateTime;LastTaskResult=1;NumberOfMissedRuns=4}}
    function Export-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);return $fixture.fleet[$TaskName].xml}
    function Register-ScheduledTask {param($TaskPath,$TaskName,$Xml,[switch]$Force,$ErrorAction);$fixture.registers++;$fixture.fleet[$TaskName].xml=$Xml}
    function Enable-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);$fixture.enables++;throw 'Maintenance must never enable a task'}
    $owner={$fixture.owners++}.GetNewClosure()
    $observed=Invoke-CreRuntimeMaintenanceTasks $release 'Observe' $root $owner
    Check ($observed.tasks.Count -eq 3 -and $fixture.registers -eq 0) 'Observe must be read-only'
    $hold=Join-Path $root 'hold';[void][IO.Directory]::CreateDirectory($hold)
    $marker=@{schema='cre-runtime-maintenance-mutation-v1';release_sha256=('c'*64);operation_output=$hold}
    Invoke-CreRuntimeMaintenanceTasks $release 'Hold' $hold $owner $marker
    Check ((Test-Path (Join-Path $hold 'mutation-start.json')) -and (Test-Path (Join-Path $hold 'scheduler-watermark.json'))) 'Hold must preserve mutation and Scheduler boundaries'
    $heldMarker=Read-CreHandoffJson ([IO.File]::ReadAllText((Join-Path $hold 'mutation-start.json')))
    Check ($heldMarker.entered_at -is [string] -and [DateTimeOffset]::Parse($heldMarker.entered_at).UtcDateTime -le [DateTime]::UtcNow) 'Mutation marker must carry the actual native publication clock'
    Check ($fixture.registers -eq 0 -and $fixture.enables -eq 0) 'Hold must not register or enable'
    $fixture.instances=@(@{key='qualified';instance='synthetic-live'})
    Refuses {Invoke-CreRuntimeMaintenanceTasks $release 'Repin' $hold $owner} 'Scheduled instance'
    Check ($fixture.registers -eq 0) 'A live instance must refuse before any repin'
    $fixture.instances=@()
    $fixture.fleet['Qualified Brokerage Pipeline'].xml=$xml.Replace('C:/runtime','C:/drift')
    Refuses {Invoke-CreRuntimeMaintenanceTasks $release 'Repin' $hold $owner} 'Complete task definition changed'
    Check ($fixture.registers -eq 0) 'Whole fleet prevalidation must precede every repin'
    $fixture.fleet['Qualified Brokerage Pipeline'].xml=$xml
    Invoke-CreRuntimeMaintenanceTasks $release 'Repin' $hold $owner
    Check ($fixture.registers -eq 3 -and $fixture.enables -eq 0) 'Repin must register exactly the three held tasks'
    foreach($t in $fixture.fleet.Values) {Check ($t.xml -ceq $xml.Replace($old,$new) -and -not $t.enabled) 'Only the code pin may change'}
    $readback=Join-Path $root 'readback';[void][IO.Directory]::CreateDirectory($readback)
    $final=Invoke-CreRuntimeMaintenanceTasks $release 'Postreadback' $readback $owner $null $hold
    Check ($final.tasks.Count -eq 3 -and $final.execution.fixture) 'Postreadback must bind the held fleet and Scheduler evidence'
    Check ($fixture.enables -eq 0 -and $fixture.owners -ge 5) 'Owner must be rechecked and maintenance never enables'
    Refuses {Invoke-CreRuntimeMaintenanceTasks $release 'Restore' $readback $owner} 'Unsupported maintenance task mode'
    Write-Output 'CRE runtime maintenance mocked native fixture: passed'
} finally {
    $resolved=[IO.Path]::GetFullPath($root)
    $temp=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if(-not $resolved.StartsWith($temp,[StringComparison]::OrdinalIgnoreCase) -or $resolved -ceq $temp) {throw 'Unsafe fixture cleanup path'}
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
