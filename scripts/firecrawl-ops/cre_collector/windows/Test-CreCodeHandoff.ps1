# Fully mocked native consumer tests; never contact Task Scheduler or WSL.
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
$script:checks=0
function Assert-Fixture($Condition,[string]$Message) { if(-not $Condition){throw $Message};$script:checks++ }
function Refuses([scriptblock]$Action,[string]$Message) {
    $refused=$false;try{& $Action|Out-Null}catch{$refused=$true};Assert-Fixture $refused $Message
}
$old='a'*40;$new='b'*40
$xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>Reviewed '+$old+'</Description></RegistrationInfo><Principals><Principal><UserId>fixture</UserId></Principal></Principals><Actions><Exec><Command>pwsh.exe</Command><Arguments>-ExpectedSha '+$old+'</Arguments><WorkingDirectory>C:/runtime</WorkingDirectory></Exec></Actions><Settings><Enabled>true</Enabled></Settings><Triggers><CalendarTrigger><StartBoundary>2099-01-01T00:00:00</StartBoundary></CalendarTrigger></Triggers></Task>'
$pinned=Pin-ReviewedXml $xml $old $new
Assert-Fixture ($pinned -ceq $xml.Replace($old,$new)) 'Only exact SHA substitution is permitted'
Refuses {Pin-ReviewedXml ($xml.Replace('-ExpectedSha','-WrongSha')) $old $new} 'Wrong action pin must refuse'
Refuses {Pin-ReviewedXml ($xml.Replace('-ExpectedSha','-ExpectedSha '+$old+' -ExpectedSha')) $old $new} 'Duplicate action pin must refuse'
Refuses {Pin-ReviewedXml ($xml.Replace('Reviewed '+$old,'Reviewed other')) $old $new} 'Missing description pin must refuse'
Assert-Fixture ((Get-CreTaskContract $xml) -cne (Get-CreTaskContract ($xml.Replace('2099-01-01','2099-01-02')))) 'Trigger drift must remain visible'
function Get-CimInstance {
    param($ClassName,$Filter,$ErrorAction)
    if($Filter -like 'ProcessId=*') {return [pscustomobject]@{ProcessId=$PID;ParentProcessId=0}}
    return @([pscustomobject]@{ProcessId=$PID;Name='pwsh.exe';CommandLine='Invoke-CreCodeHandoff.ps1 -Mode Apply'},
             [pscustomobject]@{ProcessId=987654;Name='pwsh.exe';CommandLine='Invoke-CreGuardedTier.ps1 private-value'})
}
$frontends=@(Get-CreHandoffNativeFrontends)
Assert-Fixture ($frontends.Count -eq 1 -and $frontends[0].pid -eq 987654 -and $frontends[0].Keys.Count -eq 2) 'Native observer must exclude own ancestry and retain PID/name only'
$root=Join-Path ([IO.Path]::GetTempPath()) ('cre-handoff-fixture-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
try {
    $path=Join-Path $root 'input.json';[IO.File]::WriteAllText($path,'{}',[Text.UTF8Encoding]::new($false))
    Assert-Fixture ((ConvertTo-CreHandoffPath $path) -ceq (ConvertTo-CreHandoffPath $path.Replace('\','/'))) 'Equivalent separators must normalize'
    Save-CreHandoffExclusive (Join-Path $root 'exclusive.json') '{}'
    Refuses {Save-CreHandoffExclusive (Join-Path $root 'exclusive.json') 'changed'} 'Existing outputs must refuse replay'
    $next=[DateTime]::UtcNow.AddHours(2);$last=[DateTime]::UtcNow.AddDays(-1)
    $script:fleet=@{}
    $rows=@();$inputs=@()
    foreach($pair in @(@('interra','Interra Refresh'),@('newmark','Newmark Refresh'),@('qualified','Qualified Brokerage Pipeline'))) {
        $key=$pair[0];$name=$pair[1];$baseline=Join-Path $root ($key+'.xml')
        [IO.File]::WriteAllText($baseline,$xml,[Text.UTF8Encoding]::new($false))
        $inputs+=@{role=$key;path=$baseline;sha256=(Get-CreHandoffHash $xml);bytes=([Text.UTF8Encoding]::new($false).GetByteCount($xml))}
        $rows+=@{key=$key;task_path='\Agentic Assets\CRE\';task_name=$name;xml=$key;enabled=$true;last_task_result=0;last_run_utc=$last.ToString('yyyy-MM-ddTHH:mm:ssZ');next_run_utc=$next.ToString('yyyy-MM-ddTHH:mm:ssZ')}
        $script:fleet[$name]=@{xml=$xml;enabled=$true;state='Ready'}
    }
    $release=[pscustomobject]@{roots=@{native='C:/runtime';runtime='/home/fixture/runtime'};deployed=@{sha=$old};selected=@{sha=$new};inputs=$inputs;task_baseline=$rows}
    function Get-ScheduledTask {
        param($TaskPath,$TaskName,$ErrorAction)
        $names=if($TaskName){@($TaskName)}else{@($script:fleet.Keys)}
        foreach($name in $names) {
            $item=$script:fleet[$name]
            [pscustomobject]@{TaskPath='\Agentic Assets\CRE\';TaskName=$name;State=$item.state;Settings=[pscustomobject]@{Enabled=$item.enabled};Actions=@([pscustomobject]@{Execute='pwsh.exe';Arguments='fixture';WorkingDirectory='C:/runtime'})}
        }
    }
    function Get-ScheduledTaskInfo {param($TaskPath,$TaskName,$ErrorAction);[pscustomobject]@{LastRunTime=$last;NextRunTime=$next;LastTaskResult=0}}
    function Export-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);return $script:fleet[$TaskName].xml}
    function Disable-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);$script:fleet[$TaskName].enabled=$false;$script:fleet[$TaskName].state='Disabled';$script:fleet[$TaskName].xml=Set-CreHandoffEnabledXml $script:fleet[$TaskName].xml $false}
    function Enable-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);$script:fleet[$TaskName].enabled=$true;$script:fleet[$TaskName].state='Ready';$script:fleet[$TaskName].xml=Set-CreHandoffEnabledXml $script:fleet[$TaskName].xml $true}
    function Register-ScheduledTask {param($TaskPath,$TaskName,$Xml,[switch]$Force,$ErrorAction);$script:fleet[$TaskName].xml=$Xml}
    function Get-CreHandoffAdmission {param($Release,$Output);return @{admitted=$true}}
    function Get-CreHandoffNativeFrontends {return @()}
    $observed=Invoke-CreHandoffTasks $release 'Observe' $root {throw 'Observation must never assert ownership'}
    Assert-Fixture ($observed.tasks.Count -eq 3) 'Observe must enumerate exact three mocked tasks'
    $script:owners=0
    Invoke-CreHandoffTasks $release 'Hold' $root {$script:owners++}
    Assert-Fixture (@($script:fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Hold must disable every mocked task'
    Invoke-CreHandoffTasks $release 'Publish' $root {$script:owners++}
    Assert-Fixture (@($script:fleet.Values|Where-Object {$_.enabled}).Count -eq 3) 'Publish must restore recorded enabled states'
    Assert-Fixture ($script:owners -ge 8) 'Every mutation/restoration boundary must reassert owner'
    # A partial repin failure retains all tasks disabled, including already restored members.
    $second=Join-Path $root 'partial';[void][IO.Directory]::CreateDirectory($second)
    foreach($name in @($script:fleet.Keys)) {$script:fleet[$name].xml=$xml;$script:fleet[$name].enabled=$true;$script:fleet[$name].state='Ready'}
    Invoke-CreHandoffTasks $release 'Hold' $second {$script:owners++}
    function Register-ScheduledTask {param($TaskPath,$TaskName,$Xml,[switch]$Force,$ErrorAction);throw 'Fixture partial registration failure'}
    Refuses {Invoke-CreHandoffTasks $release 'Publish' $second {$script:owners++}} 'Partial repin failure must refuse'
    Assert-Fixture (@($script:fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Partial failure must retain fleet hold'
    $script:fleet['Unexpected CRE'] = @{xml=$xml;enabled=$false;state='Disabled'}
    Refuses {Assert-CreHandoffFleet $release} 'Unexpected disabled task must block'
} finally {
    # Only the exclusively created fixture directory is removed.
    if([IO.Path]::GetFullPath($root).StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase)) {Remove-Item -LiteralPath $root -Recurse -Force}
}
Write-Output ('CRE code handoff native fixture checks: '+$script:checks)
