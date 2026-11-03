# Synthetic Scheduler fixture: actual producer, mocked event and COM boundaries.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
$root=Join-Path ([IO.Path]::GetTempPath()) ('cre-execution-fixture-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
try {
    $tip=[pscustomobject]@{LogName='Microsoft-Windows-TaskScheduler/Operational';MachineName='fixture';RecordId=100}
    $tip|Add-Member -MemberType ScriptMethod -Name ToXml -Value {return '<fixture-anchor/>'}
    function Get-CreRestorationLogTip {return $tip}
    function Get-WinEvent {param($LogName,$FilterXPath,$MaxEvents,$ErrorAction);return $tip}
    function New-Object {param($ComObject);if($ComObject -cne 'Schedule.Service'){throw 'unexpected COM'};$s=[pscustomobject]@{};$s|Add-Member -MemberType ScriptMethod -Name Connect -Value {};return $s}
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    function Get-CreRestorationRegisteredTask {param($Service,$Baseline);$task=[pscustomobject]@{Definition=[pscustomobject]@{Principal=[pscustomobject]@{UserId=$sid}}};$task|Add-Member -MemberType ScriptMethod -Name GetInstances -Value {param($Flags);return @()};return $task}
    $watermark=@{schema='cre-restoration-watermark-v1';channel=$tip.LogName;computer=$tip.MachineName;record_id=100;record_sha256=(Get-CreHandoffHash $tip.ToXml());captured_at=[DateTime]::UtcNow.AddMinutes(-1).ToString('o')}
    [IO.File]::WriteAllText((Join-Path $root 'scheduler-watermark.json'),($watermark|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    $rows=@();$baselines=@()
    foreach($pair in @(@('interra','Interra Refresh'),@('newmark','Newmark Refresh'),@('qualified','Qualified Brokerage Pipeline'))) {
        $baselines+=@{key=$pair[0];task_path='\Agentic Assets\CRE\';task_name=$pair[1]}
        $rows+=@{key=$pair[0];enabled=$false;missed_runs=23;actions=@(@{execute='fixture.exe';arguments='fixture'});xml_sha256=('a'*64);xml_reference=@{path='C:/fixture/'+$pair[0];bytes=5;sha256=('a'*64)}}
    }
    $before=$rows|ConvertTo-Json -Depth 12 -Compress
    $result=Get-CreRestorationExecution @{task_baseline=$baselines} $root $root $rows
    $record=Read-CreHandoffJson ([IO.File]::ReadAllText($result.path))
    if($record.tasks.Count -ne 3) {throw 'task sample count differs'}
    foreach($row in $record.tasks) {if($null -ne $row.PSObject.Properties['xml_reference']) {throw 'Scheduler task sample retains xml_reference'}}
    for($i=0;$i -lt $rows.Count;$i++) {
        foreach($key in @('key','enabled','missed_runs','actions','xml_sha256')) {
            if(($record.tasks[$i].$key|ConvertTo-Json -Depth 12 -Compress) -cne ($rows[$i][$key]|ConvertTo-Json -Depth 12 -Compress)) {throw 'Scheduler task fields differ'}
        }
    }
    if(($rows|ConvertTo-Json -Depth 12 -Compress) -cne $before) {throw 'Input task rows changed'}
    'CRE Scheduler execution producer fixture: passed'
} finally {
    $absolute=[IO.Path]::GetFullPath($root);$temp=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if(-not $absolute.StartsWith($temp,[StringComparison]::OrdinalIgnoreCase) -or $absolute -ceq $temp){throw 'unsafe fixture path'}
    Remove-Item -LiteralPath $absolute -Recurse -Force
}
