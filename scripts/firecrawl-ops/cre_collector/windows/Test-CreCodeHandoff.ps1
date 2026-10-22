# Fully mocked native consumer tests; never contact Task Scheduler or WSL.
param([string]$GitExecutable)

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
$script:checks=0
function Assert-Fixture($Condition,[string]$Message) {
    if(-not $Condition) {
        # Keep bounded synthetic failure evidence in captured stderr before finally
        # deletes the exclusive fixture directory; never print task XML.
        if($fixture.root) {
            foreach($file in @(Get-ChildItem -LiteralPath $fixture.root -Recurse -Filter '*failure-hold*.json' | Select-Object -First 8)) {
                if($file.Length -le 65536) {
                    try {
                        $receipt=[IO.File]::ReadAllText($file.FullName)|ConvertFrom-Json
                        $summary=@($receipt.tasks|Select-Object -First 3 key,disable_attempted,disabled,state,error)
                        [Console]::Error.WriteLine(('Synthetic failure-hold '+$file.Name+': '+($summary|ConvertTo-Json -Depth 4 -Compress)))
                    } catch {[Console]::Error.WriteLine('Synthetic failure-hold diagnostic unavailable: '+$file.Name)}
                }
            }
        }
        throw $Message
    }
    $script:checks++
}
function Refuses([scriptblock]$Action,[string]$Message,[string]$ExpectedError) {
    $caught=$null;try{& $Action|Out-Null}catch{$caught=$_}
    Assert-Fixture ($null -ne $caught) $Message
    if($ExpectedError) {Assert-Fixture ($caught.Exception.Message.Contains($ExpectedError)) ($Message+'; unexpected error: '+$caught.Exception.Message)}
}
$fixture=@{fleet=@{};disableCalls=0;failDisableName='';foreignFrontend=$true;owners=0;root=$null}
foreach($clockText in @('2001-02-03T04:05:06Z','2001-02-03T04:05:06.1234567+05:30')) {
    $clockWire=Read-CreHandoffJson ('{"clock":"'+$clockText+'","nested":[{"clock":"'+$clockText+'"}]}')
    Assert-Fixture ($clockWire.clock -is [string] -and $clockWire.clock -ceq $clockText -and $clockWire.nested[0].clock -is [string] -and $clockWire.nested[0].clock -ceq $clockText) 'JSON reader must retain exact ISO clock types and lexemes at every depth'
}
$old='a'*40;$new='b'*40
$xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>Reviewed '+$old+'</Description></RegistrationInfo><Principals><Principal><UserId>fixture</UserId></Principal></Principals><Actions><Exec><Command>pwsh.exe</Command><Arguments>-ExpectedSha '+$old+'</Arguments><WorkingDirectory>C:/runtime</WorkingDirectory></Exec></Actions><Settings><Enabled>true</Enabled></Settings><Triggers><CalendarTrigger><StartBoundary>2099-01-01T00:00:00</StartBoundary></CalendarTrigger></Triggers></Task>'
$pinned=Pin-ReviewedXml $xml $old $new
Assert-Fixture ($pinned -ceq $xml.Replace($old,$new)) 'Only exact SHA substitution is permitted'
Refuses {Pin-ReviewedXml ($xml.Replace('-ExpectedSha','-WrongSha')) $old $new} 'Wrong action pin must refuse'
Refuses {Pin-ReviewedXml ($xml.Replace('-ExpectedSha','-ExpectedSha '+$old+' -ExpectedSha')) $old $new} 'Duplicate action pin must refuse'
Refuses {Pin-ReviewedXml ($xml.Replace('Reviewed '+$old,'Reviewed other')) $old $new} 'Missing description pin must refuse'
Assert-Fixture ((Get-CreTaskContract $xml) -cne (Get-CreTaskContract ($xml.Replace('2099-01-01','2099-01-02')))) 'Trigger drift must remain visible'
Set-Item Function:Get-CimInstance -Value ({
    param($ClassName,$Filter,$ErrorAction)
    if($Filter -like 'ProcessId=*') {return [pscustomobject]@{ProcessId=$PID;ParentProcessId=0}}
    $own=[pscustomobject]@{ProcessId=$PID;Name='pwsh.exe';CommandLine='Invoke-CreCodeHandoff.ps1 -Mode Apply'}
    if($fixture.foreignFrontend) {return @($own,[pscustomobject]@{ProcessId=987654;Name='pwsh.exe';CommandLine='Invoke-CreGuardedTier.ps1 private-value'})}
    return @($own)
}.GetNewClosure())
$frontends=@(Get-CreHandoffNativeFrontends)
Assert-Fixture ($frontends.Count -eq 1 -and $frontends[0].pid -eq 987654 -and $frontends[0].Keys.Count -eq 2) 'Native observer must exclude own ancestry and retain PID/name only'
$fixture.foreignFrontend=$false
$root=Join-Path ([IO.Path]::GetTempPath()) ('cre-handoff-fixture-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
$fixture.root=$root
try {
    $path=Join-Path $root 'input.json';[IO.File]::WriteAllText($path,'{}',[Text.UTF8Encoding]::new($false))
    Assert-Fixture ((ConvertTo-CreHandoffPath $path) -ceq (ConvertTo-CreHandoffPath $path.Replace('\','/'))) 'Equivalent separators must normalize'
    Save-CreHandoffExclusive (Join-Path $root 'exclusive.json') '{}'
    Refuses {Save-CreHandoffExclusive (Join-Path $root 'exclusive.json') 'changed'} 'Existing outputs must refuse replay'
    $next=[DateTime]::UtcNow.AddHours(2);$last=[DateTime]::UtcNow.AddDays(-1)
    $rows=@();$inputs=@()
    foreach($pair in @(@('interra','Interra Refresh'),@('newmark','Newmark Refresh'),@('qualified','Qualified Brokerage Pipeline'))) {
        $key=$pair[0];$name=$pair[1];$baseline=Join-Path $root ($key+'.xml')
        [IO.File]::WriteAllText($baseline,$xml,[Text.UTF8Encoding]::new($false))
        $inputs+=@{role=$key;path=$baseline;sha256=(Get-CreHandoffHash $xml);bytes=([Text.UTF8Encoding]::new($false).GetByteCount($xml))}
        $rows+=@{key=$key;task_path='\Agentic Assets\CRE\';task_name=$name;xml=$key;enabled=$true;last_task_result=0;last_run_utc=$last.ToString('yyyy-MM-ddTHH:mm:ssZ');next_run_utc=$next.ToString('yyyy-MM-ddTHH:mm:ssZ')}
        $fixture.fleet[$name]=@{xml=$xml;enabled=$true;state='Ready'}
    }
    $sourceRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..')).Replace('\','/')
    $release=[pscustomobject]@{schema='cre-code-release-v1';roots=@{native=(Join-Path $root 'native-runtime');runtime='/home/fixture/runtime';tooling=$sourceRoot};tooling=@{sha=('c'*40);tree=('d'*40)};deployed=@{sha=$old};selected=@{sha=$new};inputs=$inputs;task_baseline=$rows;allowed_paths=@('scripts/firecrawl-ops/cre_collector/fixture.py')}
    Set-Item Function:Get-ScheduledTask -Value ({
        param($TaskPath,$TaskName,$ErrorAction)
        $names=if($TaskName){@($TaskName)}else{@($fixture.fleet.Keys)}
        foreach($name in $names) {
            $item=$fixture.fleet[$name]
            [pscustomobject]@{TaskPath='\Agentic Assets\CRE\';TaskName=$name;State=$item.state;Settings=[pscustomobject]@{Enabled=$item.enabled};Actions=@([pscustomobject]@{Execute='pwsh.exe';Arguments='fixture';WorkingDirectory='C:/runtime'})}
        }
    }.GetNewClosure())
    # Disabled next-run values are synthetic fixture behavior, not Windows acceptance.
    Set-Item Function:Get-ScheduledTaskInfo -Value ({param($TaskPath,$TaskName,$ErrorAction);[pscustomobject]@{LastRunTime=$last;NextRunTime=$(if($fixture.fleet[$TaskName].enabled){$next}else{[DateTime]::MinValue});LastTaskResult=0}}.GetNewClosure())
    Set-Item Function:Export-ScheduledTask -Value ({param($TaskPath,$TaskName,$ErrorAction);return $fixture.fleet[$TaskName].xml}.GetNewClosure())
    # Bind both mutable state and the existing pure XML helper across child scripts.
    $enabledXml=${function:Set-CreHandoffEnabledXml}
    Set-Item Function:Disable-ScheduledTask -Value ({param($TaskPath,$TaskName,$ErrorAction);$fixture.disableCalls++;if($TaskName -ceq $fixture.failDisableName){throw 'injected per-task disable failure'};$fixture.fleet[$TaskName].enabled=$false;$fixture.fleet[$TaskName].state='Disabled';$fixture.fleet[$TaskName].xml=& $enabledXml $fixture.fleet[$TaskName].xml $false}.GetNewClosure())
    Set-Item Function:Enable-ScheduledTask -Value ({param($TaskPath,$TaskName,$ErrorAction);$fixture.fleet[$TaskName].enabled=$true;$fixture.fleet[$TaskName].state='Ready';$fixture.fleet[$TaskName].xml=& $enabledXml $fixture.fleet[$TaskName].xml $true}.GetNewClosure())
    Set-Item Function:Register-ScheduledTask -Value ({param($TaskPath,$TaskName,$Xml,[switch]$Force,$ErrorAction);$fixture.fleet[$TaskName].xml=$Xml}.GetNewClosure())
    function Get-CreHandoffAdmission {param($Release,$Output,$Stage);return @{admitted=$true;cpuPercent=20;availablePhysicalGiB=8}}
    function Get-CreHandoffNativeFrontends {return @()}
    $observed=Invoke-CreHandoffTasks $release 'Observe' $root {throw 'Observation must never assert ownership'}
    Assert-Fixture ($observed.tasks.Count -eq 3) 'Observe must enumerate exact three mocked tasks'
    $fixture.owners=0
    $authPath=Join-Path $root 'authorization.json';[IO.File]::WriteAllText($authPath,'{}',[Text.UTF8Encoding]::new($false))
    $releaseHash='e'*64
    function New-FixtureOperation([string]$Output) {return [ordered]@{schema='cre-code-mutation-v1';release_sha256=$releaseHash;authorization_sha256=(Get-CreHandoffHash '{}');operation_output=(ConvertTo-CreHandoffPath $Output);tooling=$release.tooling;fleet=@($release.task_baseline)}}
    $owner={$fixture.owners++}.GetNewClosure()
    Invoke-CreHandoffTasks $release 'Hold' $root $owner (New-FixtureOperation $root)
    Assert-Fixture (@($fixture.fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Hold must disable every mocked task'
    Invoke-CreHandoffTasks $release 'Repin' $root $owner
    Invoke-CreHandoffTasks $release 'Restore' $root $owner
    Assert-Fixture (@($fixture.fleet.Values|Where-Object {$_.enabled}).Count -eq 3) 'Publish must restore recorded enabled states'
    Assert-Fixture ($fixture.owners -ge 8) 'Every mutation/restoration boundary must reassert owner'
    # A partial repin failure retains all tasks disabled, including already restored members.
    $second=Join-Path $root 'partial';[void][IO.Directory]::CreateDirectory($second)
    foreach($name in @($fixture.fleet.Keys)) {$fixture.fleet[$name].xml=$xml;$fixture.fleet[$name].enabled=$true;$fixture.fleet[$name].state='Ready'}
    Invoke-CreHandoffTasks $release 'Hold' $second $owner (New-FixtureOperation $second)
    function Register-ScheduledTask {param($TaskPath,$TaskName,$Xml,[switch]$Force,$ErrorAction);throw 'Fixture partial registration failure'}
    Refuses {Invoke-CreHandoffTasks $release 'Repin' $second $owner} 'Partial repin failure must refuse' 'Fixture partial registration failure'
    $cleanup=Invoke-CreHandoffFailureHold $release $second $releaseHash $authPath
    Assert-Fixture ($cleanup.tasks.Count -eq 3 -and @($cleanup.tasks|Where-Object {-not $_.disabled}).Count -eq 0) 'Dedicated failure hold must attempt every inventoried task'
    $fixture.failDisableName='Newmark Refresh'
    $uncertain=Invoke-CreHandoffFailureHold $release $second $releaseHash $authPath 'failure-hold-uncertain.json'
    Assert-Fixture (@($uncertain.tasks|Where-Object {$_.disable_attempted}).Count -eq 3 -and @($uncertain.tasks|Where-Object {$_.error}).Count -eq 1) 'One disable failure must retain per-task uncertainty while attempting the others'
    $fixture.failDisableName=''
    Assert-Fixture (@($fixture.fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Partial failure must retain fleet hold'
    $fixture.fleet['Unexpected CRE'] = @{xml=$xml;enabled=$false;state='Disabled'}
    Refuses {Assert-CreHandoffFleet $release} 'Unexpected disabled task must block'
    $fixture.fleet.Remove('Unexpected CRE')
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
    # Traverse actual JSON ingestion and foreground Observe/Hold dispatch using
    # the same bound mocks; no real WSL owner or task is contacted.
    function wsl.exe {$global:LASTEXITCODE=0}
    foreach($name in @($fixture.fleet.Keys)) {$fixture.fleet[$name].xml=$xml;$fixture.fleet[$name].enabled=$true;$fixture.fleet[$name].state='Ready'}
    $serializedObserve=Join-Path $root 'serialized-observe';[void][IO.Directory]::CreateDirectory($serializedObserve)
    & $frontdoor -Mode Observe -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $serializedObserve
    $wireObserved=Read-CreHandoffJson ([IO.File]::ReadAllText((Join-Path $serializedObserve 'native-observation.json')))
    Assert-Fixture ($wireObserved.tasks.Count -eq 3 -and @($wireObserved.tasks|Where-Object {$_.last_run_utc -isnot [string] -or $_.last_run_utc -cne $rows[0].last_run_utc -or $_.next_run_utc -isnot [string] -or $_.next_run_utc -cne $rows[0].next_run_utc}).Count -eq 0) 'Serialized actual Observe must preserve exact reviewed clocks'
    foreach($clockField in @('last_run_utc','next_run_utc')) {
        $drift=Read-CreHandoffJson $wire
        $drift.task_baseline[0].$clockField=$drift.task_baseline[0].$clockField.Replace('Z','+00:00')
        $driftWire=$drift|ConvertTo-Json -Depth 15
        $driftPath=Join-Path $root ($clockField+'-drift.json');[IO.File]::WriteAllText($driftPath,$driftWire,[Text.UTF8Encoding]::new($false))
        $driftOutput=Join-Path $root ($clockField+'-drift');[void][IO.Directory]::CreateDirectory($driftOutput)
        Refuses {& $frontdoor -Mode Observe -ReleasePath $driftPath -ReleaseSha (Get-CreHandoffHash $driftWire) -Output $driftOutput} 'Equivalent instant with changed wire clock must still refuse' 'Task history/result/clocks changed'
    }
    $serializedHold=Join-Path $root 'serialized-hold';[void][IO.Directory]::CreateDirectory($serializedHold)
    $beforeSerializedHold=$fixture.disableCalls
    & $frontdoor -Mode Hold -OwnerPid 123 -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $serializedHold -Observation $path -Authorization $authPath
    $holdMarker=Read-CreHandoffJson ([IO.File]::ReadAllText((Join-Path $serializedHold 'mutation-start.json')))
    Assert-Fixture ((Test-Path -LiteralPath (Join-Path $serializedHold 'native-hold.json')) -and $fixture.disableCalls -eq $beforeSerializedHold+3 -and @($fixture.fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Serialized actual Hold must publish its receipt and disable exactly three mocked tasks'
    Assert-Fixture ($holdMarker.fleet[0].last_run_utc -is [string] -and $holdMarker.fleet[0].last_run_utc -ceq $rows[0].last_run_utc) 'Mutation marker must retain original reviewed clock strings'
    foreach($name in @($fixture.fleet.Keys)) {$fixture.fleet[$name].xml=$xml;$fixture.fleet[$name].enabled=$true;$fixture.fleet[$name].state='Ready'}
    $unadmitted=Join-Path $root 'unadmitted-operation'
    $priorDisables=$fixture.disableCalls
    [CreWindowsCapture.Capture]::ThrowAfterStart=$true
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $unadmitted -Observation $path -Authorization $authPath} 'Pre-admission capture error must refuse' 'injected post-start capture exception'
    Assert-Fixture (-not (Test-Path -LiteralPath (Join-Path $unadmitted 'mutation-start.json'))) 'Pre-admission failure has no mutation-start witness'
    Assert-Fixture ($fixture.disableCalls -eq $priorDisables) 'No mutation-start means ZERO failure-cleanup task changes'
    Assert-Fixture (@($fixture.fleet.Values|Where-Object {$_.enabled -and $_.state -ceq 'Ready'}).Count -eq 3) 'Pre-admission rejection must preserve all three healthy mocked schedules'
    $started=Join-Path $root 'started-operation'
    [CreWindowsCapture.Capture]::MutationMarkerPath=Join-Path $started 'mutation-start.json'
    [CreWindowsCapture.Capture]::MutationMarkerJson=(New-FixtureOperation $started)|ConvertTo-Json -Depth 15
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $started -Observation $path -Authorization $authPath} 'Actual outer post-start capture exception must refuse and retain original failure' 'injected post-start capture exception'
    Assert-Fixture ([IO.File]::ReadAllText((Join-Path $started 'mutation-start.json')) -ceq [CreWindowsCapture.Capture]::MutationMarkerJson) 'Capture must reach the exact admitted mutation marker'
    Assert-Fixture ($fixture.disableCalls -eq $priorDisables+3) 'Outer capture exception must hold exactly the inventoried fleet'
    Assert-Fixture (Test-Path -LiteralPath (Join-Path $started 'outer-failure-hold.json')) 'Outer cleanup uncertainty must be durable'
    $startedHold=[IO.File]::ReadAllText((Join-Path $started 'outer-failure-hold.json'))|ConvertFrom-Json
    Assert-Fixture ($startedHold.tasks.Count -eq 3 -and @($startedHold.tasks|Where-Object {-not $_.disable_attempted -or -not $_.disabled -or $_.error}).Count -eq 0) 'Post-start cleanup must report all three actual mocked disables successful'
    $startedFailure=[IO.File]::ReadAllText($started+'.outer-failure.json')|ConvertFrom-Json
    Assert-Fixture ($startedFailure.original_error.Contains('injected post-start capture exception')) 'Original injected capture error must survive cleanup publication'
    [CreWindowsCapture.Capture]::ThrowAfterStart=$false
    [CreWindowsCapture.Capture]::BadEof=$true
    $eof=Join-Path $root 'postcommit-eof'
    [CreWindowsCapture.Capture]::MutationMarkerPath=Join-Path $eof 'mutation-start.json'
    [CreWindowsCapture.Capture]::MutationMarkerJson=(New-FixtureOperation $eof)|ConvertTo-Json -Depth 15
    $beforeEof=$fixture.disableCalls
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $eof -Observation $path -Authorization $authPath} 'EOF uncertainty after Linux exit must never be promoted to success' 'Native foreground exit/drain uncertain'
    Assert-Fixture ([IO.File]::ReadAllText((Join-Path $eof 'mutation-start.json')) -ceq [CreWindowsCapture.Capture]::MutationMarkerJson) 'EOF fault must follow the exact mutation marker'
    Assert-Fixture ($fixture.disableCalls -eq $beforeEof+3) 'EOF cleanup must attempt exactly three mocked disables'
    Assert-Fixture (Test-Path -LiteralPath (Join-Path $eof 'outer-failure-hold.json')) 'Postcommit EOF failure must attempt exact task hold without asserting retained recovery'
    $eofHold=[IO.File]::ReadAllText((Join-Path $eof 'outer-failure-hold.json'))|ConvertFrom-Json
    Assert-Fixture ($eofHold.tasks.Count -eq 3 -and @($eofHold.tasks|Where-Object {-not $_.disable_attempted -or -not $_.disabled -or $_.error}).Count -eq 0) 'EOF cleanup must report all three mocked tasks held without task errors'
    [CreWindowsCapture.Capture]::BadEof=$false
    [CreWindowsCapture.Capture]::MutationMarkerPath=$null
    $success=Join-Path $root 'capture-complete'
    & $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $success -Observation $path -Authorization $authPath
    $completion=[IO.File]::ReadAllText($success+'.native.json')|ConvertFrom-Json
    Assert-Fixture ($completion.schema -ceq 'cre-code-native-completion-v1' -and $completion.capture.Stdout.Eof) 'Actual native frontdoor must publish real adapter result shape; this fixture is not runtime acceptance'
    foreach($name in @($fixture.fleet.Keys)) {$fixture.fleet[$name].xml=$xml;$fixture.fleet[$name].enabled=$true;$fixture.fleet[$name].state='Ready'}
    $beforeReplay=$fixture.disableCalls
    Refuses {& $frontdoor -Mode Apply -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $success -Observation $path -Authorization $authPath} 'Replay of prior output must refuse before capture/cleanup' 'Exclusive operation output already exists'
    Assert-Fixture ($fixture.disableCalls -eq $beforeReplay) 'Replay refusal must never disable healthy schedules'
    Assert-Fixture (@($fixture.fleet.Values|Where-Object {$_.enabled -and $_.state -ceq 'Ready'}).Count -eq 3) 'Replay must preserve all three healthy mocked schedules'
    foreach($name in @($fixture.fleet.Keys)) {$fixture.fleet[$name].xml=$xml;$fixture.fleet[$name].enabled=$true;$fixture.fleet[$name].state='Ready'}
    $receiptFailure=Join-Path $root 'native-receipt-failure';[void][IO.Directory]::CreateDirectory($receiptFailure)
    Save-CreHandoffExclusive (Join-Path $receiptFailure 'native-hold.json') 'pre-existing output must refuse'
    Refuses {& $frontdoor -Mode Hold -OwnerPid 123 -ReleasePath $frontdoorRelease -ReleaseSha $releaseHash -Output $receiptFailure -Observation $path -Authorization $authPath} 'Native receipt failure after function return must propagate' 'native-hold.json'
    Assert-Fixture (Test-Path -LiteralPath (Join-Path $receiptFailure 'mutation-start.json')) 'Mutation provenance must survive native receipt failure'
    Assert-Fixture (@($fixture.fleet.Values|Where-Object {$_.enabled}).Count -eq 0) 'Native receipt failure must leave exact fleet held for enclosing Linux cleanup'
} finally {
    # Only the exclusively created fixture directory is removed.
    if([IO.Path]::GetFullPath($root).StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase)) {Remove-Item -LiteralPath $root -Recurse -Force}
}
Write-Output ('CRE code handoff native fixture checks: '+$script:checks)
