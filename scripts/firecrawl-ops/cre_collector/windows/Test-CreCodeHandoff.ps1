# Fully mocked native consumer tests; never contact Task Scheduler or WSL.
param([string]$GitExecutable)

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
    $own=[pscustomobject]@{ProcessId=$PID;Name='pwsh.exe';CommandLine='Invoke-CreCodeHandoff.ps1 -Mode Apply'}
    if($script:foreignFrontend) {return @($own,[pscustomobject]@{ProcessId=987654;Name='pwsh.exe';CommandLine='Invoke-CreGuardedTier.ps1 private-value'})}
    return @($own)
}
$script:foreignFrontend=$true
$frontends=@(Get-CreHandoffNativeFrontends)
Assert-Fixture ($frontends.Count -eq 1 -and $frontends[0].pid -eq 987654 -and $frontends[0].Keys.Count -eq 2) 'Native observer must exclude own ancestry and retain PID/name only'
$script:foreignFrontend=$false
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
    $sourceRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..')).Replace('\','/')
    $release=[pscustomobject]@{schema='cre-code-release-v1';roots=@{native='C:/runtime';runtime='/home/fixture/runtime';tooling=$sourceRoot};tooling=@{sha=('c'*40);tree=('d'*40)};deployed=@{sha=$old};selected=@{sha=$new};inputs=$inputs;task_baseline=$rows}
    function Get-ScheduledTask {
        param($TaskPath,$TaskName,$ErrorAction)
        $names=if($TaskName){@($TaskName)}else{@($script:fleet.Keys)}
        foreach($name in $names) {
            $item=$script:fleet[$name]
            [pscustomobject]@{TaskPath='\Agentic Assets\CRE\';TaskName=$name;State=$item.state;Settings=[pscustomobject]@{Enabled=$item.enabled};Actions=@([pscustomobject]@{Execute='pwsh.exe';Arguments='fixture';WorkingDirectory='C:/runtime'})}
        }
    }
    function Get-ScheduledTaskInfo {param($TaskPath,$TaskName,$ErrorAction);[pscustomobject]@{LastRunTime=$last;NextRunTime=$(if($script:fleet[$TaskName].enabled){$next}else{[DateTime]::MinValue});LastTaskResult=0}}
    function Export-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);return $script:fleet[$TaskName].xml}
    $script:disableCalls=0
    $script:failDisableName=''
    function Disable-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);$script:disableCalls++;if($TaskName -ceq $script:failDisableName){throw 'injected per-task disable failure'};$script:fleet[$TaskName].enabled=$false;$script:fleet[$TaskName].state='Disabled';$script:fleet[$TaskName].xml=Set-CreHandoffEnabledXml $script:fleet[$TaskName].xml $false}
    function Enable-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction);$script:fleet[$TaskName].enabled=$true;$script:fleet[$TaskName].state='Ready';$script:fleet[$TaskName].xml=Set-CreHandoffEnabledXml $script:fleet[$TaskName].xml $true}
    function Register-ScheduledTask {param($TaskPath,$TaskName,$Xml,[switch]$Force,$ErrorAction);$script:fleet[$TaskName].xml=$Xml}
    function Get-CreHandoffAdmission {param($Release,$Output,$Stage);return @{admitted=$true;cpuPercent=20;availablePhysicalGiB=8}}
    function Get-CreHandoffNativeFrontends {return @()}
    $observed=Invoke-CreHandoffTasks $release 'Observe' $root {throw 'Observation must never assert ownership'}
    Assert-Fixture ($observed.tasks.Count -eq 3) 'Observe must enumerate exact three mocked tasks'
    $script:owners=0
    $authPath=Join-Path $root 'authorization.json';[IO.File]::WriteAllText($authPath,'{}',[Text.UTF8Encoding]::new($false))
    $releaseHash='e'*64
    function New-FixtureOperation([string]$Output) {return [ordered]@{schema='cre-code-mutation-v1';release_sha256=$releaseHash;authorization_sha256=(Get-CreHandoffHash '{}');operation_output=(ConvertTo-CreHandoffPath $Output);tooling=$release.tooling;fleet=@($release.task_baseline)}}
    Invoke-CreHandoffTasks $release 'Hold' $root {$script:owners++} (New-FixtureOperation $root)
    Assert-Fixture (@($script:fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Hold must disable every mocked task'
    Invoke-CreHandoffTasks $release 'Repin' $root {$script:owners++}
    Invoke-CreHandoffTasks $release 'Restore' $root {$script:owners++}
    Assert-Fixture (@($script:fleet.Values|Where-Object {$_.enabled}).Count -eq 3) 'Publish must restore recorded enabled states'
    Assert-Fixture ($script:owners -ge 8) 'Every mutation/restoration boundary must reassert owner'
    # A partial repin failure retains all tasks disabled, including already restored members.
    $second=Join-Path $root 'partial';[void][IO.Directory]::CreateDirectory($second)
    foreach($name in @($script:fleet.Keys)) {$script:fleet[$name].xml=$xml;$script:fleet[$name].enabled=$true;$script:fleet[$name].state='Ready'}
    Invoke-CreHandoffTasks $release 'Hold' $second {$script:owners++} (New-FixtureOperation $second)
    function Register-ScheduledTask {param($TaskPath,$TaskName,$Xml,[switch]$Force,$ErrorAction);throw 'Fixture partial registration failure'}
    Refuses {Invoke-CreHandoffTasks $release 'Repin' $second {$script:owners++}} 'Partial repin failure must refuse'
    $cleanup=Invoke-CreHandoffFailureHold $release $second $releaseHash $authPath
    Assert-Fixture ($cleanup.tasks.Count -eq 3 -and @($cleanup.tasks|Where-Object {-not $_.disabled}).Count -eq 0) 'Dedicated failure hold must attempt every inventoried task'
    $script:failDisableName='Newmark Refresh'
    $uncertain=Invoke-CreHandoffFailureHold $release $second $releaseHash $authPath 'failure-hold-uncertain.json'
    Assert-Fixture (@($uncertain.tasks|Where-Object {$_.disable_attempted}).Count -eq 3 -and @($uncertain.tasks|Where-Object {$_.error}).Count -eq 1) 'One disable failure must retain per-task uncertainty while attempting the others'
    $script:failDisableName=''
    Assert-Fixture (@($script:fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Partial failure must retain fleet hold'
    $script:fleet['Unexpected CRE'] = @{xml=$xml;enabled=$false;state='Disabled'}
    Refuses {Assert-CreHandoffFleet $release} 'Unexpected disabled task must block'
    $script:fleet.Remove('Unexpected CRE')
    # Real shipped entrypoint: mismatch refuses before any host dispatch, while
    # a correctly bound offline preview succeeds without constructing adapters.
    $frontdoor=Join-Path $PSScriptRoot 'Invoke-CreCodeHandoff.ps1'
    $frontdoorRelease=Join-Path $root 'frontdoor-release.json'
    $release.roots.tooling=$root
    $wire=$release|ConvertTo-Json -Depth 15
    [IO.File]::WriteAllText($frontdoorRelease,$wire,[Text.UTF8Encoding]::new($false))
    Refuses {& $frontdoor -Mode Preview -ReleasePath $frontdoorRelease -ReleaseSha (Get-CreHandoffHash $wire)} 'Actual native frontdoor must reject declared/actual tooling mismatch'
    $release.roots.tooling=$sourceRoot
    $wire=$release|ConvertTo-Json -Depth 15
    [IO.File]::WriteAllText($frontdoorRelease,$wire,[Text.UTF8Encoding]::new($false))
    $preview=(& $frontdoor -Mode Preview -ReleasePath $frontdoorRelease -ReleaseSha (Get-CreHandoffHash $wire))|ConvertFrom-Json
    Assert-Fixture ($preview.preview_only -eq $true -and $preview.execution -eq $false) 'Actual correctly bound frontdoor preview must remain offline'
    # Mock ONLY the external capture and Git adapters while invoking the actual
    # Apply frontdoor. No wsl.exe/process/Docker/Task Scheduler is contacted.
    Add-Type -TypeDefinition @'
using System;
using System.IO;
namespace CreWindowsCapture {
    public class StreamResult { public bool Eof=true; public bool Truncated=false; }
    public class CaptureResult { public bool Started=true; public bool Failed=false; public int NativeExitCode=0; public StreamResult Stdout=new StreamResult(); public StreamResult Stderr=new StreamResult(); }
    public static class Capture {
        public static bool ThrowAfterStart;
        public static bool BadEof;
        public static string MutationMarkerPath;
        public static string MutationMarkerJson;
        public static CaptureResult Run(string exe,string[] args,string output,string error,string prefix) {
            if(exe!="wsl.exe")throw new Exception("Unexpected executable");
            File.WriteAllText(output,"mocked native adapter only");File.WriteAllText(error,"");
            if(MutationMarkerPath!=null) { Directory.CreateDirectory(Path.GetDirectoryName(MutationMarkerPath));File.WriteAllText(MutationMarkerPath,MutationMarkerJson); }
            if(ThrowAfterStart)throw new Exception("injected post-start capture exception");
            CaptureResult result=new CaptureResult();result.Stdout.Eof=!BadEof;return result;
        }
        public static CaptureResult RunReadOnly(string exe,string[] args,string output,string error,int deadline) {
            File.WriteAllText(output,"{\"admitted\":true,\"cpuPercent\":20,\"availablePhysicalGiB\":8}");File.WriteAllText(error,"");return new CaptureResult();
        }
    }
}
'@
    $fakeGit=Join-Path $root 'fixture-git.ps1'
    $fixtureHooks=Join-Path $root 'empty-hooks';[void][IO.Directory]::CreateDirectory($fixtureHooks)
    [IO.File]::WriteAllText($fakeGit,('if($args[-1] -ceq ''hooks''){'''+$fixtureHooks+'''}elseif($args[-1] -ceq ''core.fsmonitor''){''false''}elseif($args[-1] -ceq ''HEAD''){'''+('c'*40)+'''}elseif($args[-1] -ceq ''HEAD^{tree}''){'''+('d'*40)+'''}elseif($args -notcontains ''status''){throw ''Unexpected Git fixture request''};$global:LASTEXITCODE=0'),[Text.UTF8Encoding]::new($false))
    # An actual contained Git checkout proves the fsmonitor refuses BEFORE
    # status could dispatch the monitor. All mutations belong to this fixture.
    $priorGlobal=$env:GIT_CONFIG_GLOBAL;$priorNoSystem=$env:GIT_CONFIG_NOSYSTEM
    $emptyGlobal=Join-Path $root 'empty.gitconfig';[IO.File]::WriteAllText($emptyGlobal,'')
    $env:GIT_CONFIG_GLOBAL=$emptyGlobal;$env:GIT_CONFIG_NOSYSTEM='1'
    try {
    $realGit=if($GitExecutable) {$GitExecutable} else {(Get-Command git.exe -ErrorAction Stop).Source}
    $gitRoot=Join-Path $root 'git-admission';[void][IO.Directory]::CreateDirectory($gitRoot)
    $null=& $realGit -C $gitRoot init
    if($LASTEXITCODE -ne 0) {throw 'Fixture Git init failed'}
    [IO.File]::WriteAllText((Join-Path $gitRoot 'fixture.txt'),'offline fixture')
    $null=& $realGit -c core.fsmonitor=false -c core.hooksPath=$fixtureHooks -C $gitRoot add fixture.txt
    if($LASTEXITCODE -ne 0) {throw 'Fixture Git add failed'}
    $null=& $realGit -c core.fsmonitor=false -c core.hooksPath=$fixtureHooks -c commit.gpgsign=false -c user.name=Fixture -c user.email=fixture@example.invalid -C $gitRoot commit -m fixture
    if($LASTEXITCODE -ne 0) {throw 'Fixture Git commit failed'}
    $gitRelease=@{roots=@{tooling=$gitRoot};programs=@{native_git=$realGit};tooling=@{sha=(& $realGit -C $gitRoot rev-parse HEAD);tree=(& $realGit -C $gitRoot rev-parse 'HEAD^{tree}')}}
    $null=& $realGit -C $gitRoot config core.fsmonitor false
    $null=& $realGit -C $gitRoot config core.hooksPath $fixtureHooks
    Assert-CreHandoffTooling $gitRelease $gitRoot
    Assert-Fixture $true 'Actual clean native Git admission succeeds'
    $monitorMarker=Join-Path $root 'monitor-invoked.txt'
    $monitorCommand=Join-Path $root 'monitor.cmd'
    [IO.File]::WriteAllText($monitorCommand,('@echo off'+"`r`n"+'echo invoked > "'+$monitorMarker+'"'+"`r`n"))
    $null=& $realGit -C $gitRoot config core.fsmonitor ('"'+$monitorCommand+'"')
    Refuses {Assert-CreHandoffTooling $gitRelease $gitRoot} 'Effective fsmonitor must refuse before status'
    Assert-Fixture (-not (Test-Path -LiteralPath $monitorMarker)) 'Refused monitor must never execute'
    $null=& $realGit -C $gitRoot config core.fsmonitor false
    [IO.File]::WriteAllText((Join-Path $fixtureHooks 'post-index-change'),'fixture hook must not execute')
    Refuses {Assert-CreHandoffTooling $gitRelease $gitRoot} 'Effective hook must refuse before status'
    Remove-Item -LiteralPath (Join-Path $fixtureHooks 'post-index-change')
    } finally {$env:GIT_CONFIG_GLOBAL=$priorGlobal;$env:GIT_CONFIG_NOSYSTEM=$priorNoSystem}
    $release|Add-Member -NotePropertyName programs -NotePropertyValue @{native_git=$fakeGit; powershell=$fakeGit}
    $release|Add-Member -NotePropertyName wsl -NotePropertyValue @{distro='fixture';user='fixture'}
    $wire=$release|ConvertTo-Json -Depth 15
    [IO.File]::WriteAllText($frontdoorRelease,$wire,[Text.UTF8Encoding]::new($false));$releaseHash=Get-CreHandoffHash $wire
    $unadmitted=Join-Path $root 'unadmitted-operation'
    $priorDisables=$script:disableCalls
    [CreWindowsCapture.Capture]::ThrowAfterStart=$true
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $unadmitted -Observation $path -Authorization $authPath} 'Pre-admission capture error must refuse'
    Assert-Fixture ($script:disableCalls -eq $priorDisables) 'No mutation-start means ZERO failure-cleanup task changes'
    $started=Join-Path $root 'started-operation'
    [CreWindowsCapture.Capture]::MutationMarkerPath=Join-Path $started 'mutation-start.json'
    [CreWindowsCapture.Capture]::MutationMarkerJson=(New-FixtureOperation $started)|ConvertTo-Json -Depth 15
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $started -Observation $path -Authorization $authPath} 'Actual outer post-start capture exception must refuse and retain original failure'
    Assert-Fixture ($script:disableCalls -eq $priorDisables+3) 'Outer capture exception must hold exactly the inventoried fleet'
    Assert-Fixture (Test-Path -LiteralPath (Join-Path $started 'outer-failure-hold.json')) 'Outer cleanup uncertainty must be durable'
    [CreWindowsCapture.Capture]::ThrowAfterStart=$false
    [CreWindowsCapture.Capture]::BadEof=$true
    $eof=Join-Path $root 'postcommit-eof'
    [CreWindowsCapture.Capture]::MutationMarkerPath=Join-Path $eof 'mutation-start.json'
    [CreWindowsCapture.Capture]::MutationMarkerJson=(New-FixtureOperation $eof)|ConvertTo-Json -Depth 15
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $eof -Observation $path -Authorization $authPath} 'EOF uncertainty after Linux exit must never be promoted to success'
    Assert-Fixture (Test-Path -LiteralPath (Join-Path $eof 'outer-failure-hold.json')) 'Postcommit EOF failure must attempt exact task hold without asserting retained recovery'
    [CreWindowsCapture.Capture]::BadEof=$false
    [CreWindowsCapture.Capture]::MutationMarkerPath=$null
    $success=Join-Path $root 'capture-complete'
    & $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $success -Observation $path -Authorization $authPath
    $completion=[IO.File]::ReadAllText($success+'.native.json')|ConvertFrom-Json
    Assert-Fixture ($completion.schema -ceq 'cre-code-native-completion-v1' -and $completion.capture.Stdout.Eof) 'Actual native frontdoor must publish real adapter result shape; this fixture is not runtime acceptance'
    $beforeReplay=$script:disableCalls
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $success -Observation $path -Authorization $authPath} 'Replay of prior output must refuse before capture/cleanup'
    Assert-Fixture ($script:disableCalls -eq $beforeReplay) 'Replay refusal must never disable healthy schedules'
    function wsl.exe {$global:LASTEXITCODE=0}
    foreach($name in @($script:fleet.Keys)) {$script:fleet[$name].xml=$xml;$script:fleet[$name].enabled=$true;$script:fleet[$name].state='Ready'}
    $receiptFailure=Join-Path $root 'native-receipt-failure';[void][IO.Directory]::CreateDirectory($receiptFailure)
    Save-CreHandoffExclusive (Join-Path $receiptFailure 'native-hold.json') 'pre-existing output must refuse'
    Refuses {& $frontdoor -Mode Hold -OwnerPid 123 -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $receiptFailure -Observation $path -Authorization $authPath} 'Native receipt failure after function return must propagate'
    Assert-Fixture (Test-Path -LiteralPath (Join-Path $receiptFailure 'mutation-start.json')) 'Mutation provenance must survive native receipt failure'
    Assert-Fixture (@($script:fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Native receipt failure must leave exact fleet held for enclosing Linux cleanup'
} finally {
    # Only the exclusively created fixture directory is removed.
    if([IO.Path]::GetFullPath($root).StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase)) {Remove-Item -LiteralPath $root -Recurse -Force}
}
Write-Output ('CRE code handoff native fixture checks: '+$script:checks)
