# Offline native restoration fault proof. No real task or child process dispatch.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$GitExecutable
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if(Test-Path -LiteralPath $Output){throw 'Exclusive native proof output required'}
$null=New-Item -ItemType Directory -Path $Output
$Output=[IO.Path]::GetFullPath($Output)
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')

# Exercise the actual execution producer with a Schedule.Service mock. Installed
# TaskPath retains its trailing separator; the COM API must receive none.
$producerOutput=Join-Path $Output 'scheduler-producer'
$null=New-Item -ItemType Directory -Path $producerOutput
$script:schedulerFolders=[Collections.Generic.List[string]]::new()
$script:schedulerTasks=[Collections.Generic.List[string]]::new()
$script:schedulerTip=[pscustomobject]@{LogName='Microsoft-Windows-TaskScheduler/Operational';MachineName='fixture-host';RecordId=100}
$script:schedulerTip|Add-Member -MemberType ScriptMethod -Name ToXml -Value {return '<Event>offline-fixture-anchor</Event>'}
$script:schedulerRegistered=[pscustomobject]@{Definition=[pscustomobject]@{Principal=[pscustomobject]@{UserId=[Security.Principal.WindowsIdentity]::GetCurrent().Name}}}
$script:schedulerRegistered|Add-Member -MemberType ScriptMethod -Name GetInstances -Value {param([int]$Flags) if($Flags -ne 0){throw 'Unexpected instance flag'};return @()}
$script:schedulerFolder=[pscustomobject]@{}
$script:schedulerFolder|Add-Member -MemberType ScriptMethod -Name GetTask -Value {
    param([string]$TaskName)
    if($TaskName -cnotin @('Interra Refresh','Newmark Refresh','Qualified Brokerage Pipeline')){throw 'Foreign mock task lookup'}
    $script:schedulerTasks.Add($TaskName)
    return $script:schedulerRegistered
}
$script:schedulerService=[pscustomobject]@{}
$script:schedulerService|Add-Member -MemberType ScriptMethod -Name Connect -Value {}
$script:schedulerService|Add-Member -MemberType ScriptMethod -Name GetFolder -Value {
    param([string]$FolderPath)
    $script:schedulerFolders.Add($FolderPath)
    if($FolderPath -cne '\Agentic Assets\CRE'){throw 'COM mock rejects trailing or foreign folder syntax'}
    return $script:schedulerFolder
}
function New-Object {param([string]$ComObject) if($ComObject -cne 'Schedule.Service'){throw 'Foreign COM factory'};return $script:schedulerService}
function Get-CreRestorationLogTip {return $script:schedulerTip}
function Get-WinEvent {param([string]$LogName,[string]$FilterXPath,[int]$MaxEvents,$ErrorAction) return $script:schedulerTip}
$schedulerRows=@(
    [pscustomobject]@{key='interra';task_path='\Agentic Assets\CRE\';task_name='Interra Refresh'},
    [pscustomobject]@{key='newmark';task_path='\Agentic Assets\CRE\';task_name='Newmark Refresh'},
    [pscustomobject]@{key='qualified';task_path='\Agentic Assets\CRE\';task_name='Qualified Brokerage Pipeline'}
)
$originalRows=$schedulerRows|ConvertTo-Json -Depth 5 -Compress
Save-CreRestorationWatermark $producerOutput
$produced=Get-CreRestorationExecution @{task_baseline=$schedulerRows} $producerOutput $producerOutput $schedulerRows
$producerRecord=Read-CreHandoffJson ([IO.File]::ReadAllText($produced.path))
if($script:schedulerFolders.Count -ne 3 -or $script:schedulerTasks.Count -ne 3 -or @($producerRecord.principals).Count -ne 3){throw 'Original fleet producer lookup incomplete'}
if(($schedulerRows|ConvertTo-Json -Depth 5 -Compress) -cne $originalRows -or ($producerRecord.tasks|ConvertTo-Json -Depth 5 -Compress) -cne $originalRows){throw 'COM lookup changed installed task identities'}
$badPaths=@('\Foreign\','\','\Agentic Assets\CRE\\')
for($case=0;$case -lt 4;$case++) {
    $negative=Join-Path $producerOutput ('negative-'+$case)
    $null=New-Item -ItemType Directory -Path $negative
    $negativeRows=@(foreach($row in $schedulerRows){[pscustomobject]@{key=$row.key;task_path=$row.task_path;task_name=$row.task_name}})
    if($case -lt 3){$negativeRows[0].task_path=$badPaths[$case]}else{$negativeRows[0].task_name='Foreign Refresh'}
    Save-CreRestorationWatermark $negative
    $before=$script:schedulerFolders.Count;$rejected=$false
    try {$null=Get-CreRestorationExecution @{task_baseline=$negativeRows} $negative $negative $negativeRows}
    catch {$rejected=$_.Exception.Message -like '*outside original restoration fleet*'}
    if(-not $rejected -or $script:schedulerFolders.Count -ne $before){throw 'Out-of-scope COM lookup was attempted'}
}
Remove-Item Function:\New-Object,Function:\Get-WinEvent,Function:\Get-CreRestorationLogTip
. (Join-Path $PSScriptRoot 'CreRestorationExecution.ps1')

$script:disableCount=0;$script:leaseCount=0
function Disable-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction) $script:disableCount++}
function Get-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction) return @{State='Disabled';Settings=@{Enabled=$false}}}
function Get-CreHandoffNativeFrontends {return @()}
function Read-CreHandoffBaselineXml {param($Release,$Baseline) return '<Task/>'}
$rows=@(foreach($key in @('interra','newmark','qualified')) {@{key=$key;task_path='\Synthetic\';task_name=$key}})
$release=@{schema='cre-code-restoration-v1';task_baseline=$rows;tooling=@{sha=('a'*40);tree=('b'*40)}}
$auth=Join-Path $Output 'expired-authorization.json'
Save-CreHandoffExclusive $auth '{"expires_at":"2000-01-01T00:00:00Z"}'
$operation=Join-Path $Output 'owned-operation'
$null=New-Item -ItemType Directory -Path $operation
$marker=@{schema='cre-code-restoration-mutation-v1';restoration_sha256=('c'*64);
    authorization_sha256=(Get-CreHandoffHash ([IO.File]::ReadAllText($auth)));
    operation_output=$operation.Replace('\','/');tooling=$release.tooling;fleet=$rows}
Save-CreHandoffExclusive (Join-Path $operation 'mutation-start.json') ($marker|ConvertTo-Json -Depth 15)
$receipt=Invoke-CreHandoffFailureHold $release $operation ('c'*64) $auth -AssertOwner {$script:leaseCount++}
if(-not $receipt.attempted -or $script:disableCount -ne 3 -or $script:leaseCount -ne 4){throw 'Expired admission prevented exact owned cleanup'}
Save-CreHandoffExclusive (Join-Path $operation 'release-start.json') '{}'
$script:disableCount=0;$refused=$false
try {$null=Invoke-CreHandoffFailureHold $release $operation ('c'*64) $auth -AssertOwner {$script:leaseCount++}}
catch {$refused=$_.Exception.Message -like '*release started*'}
if(-not $refused -or $script:disableCount -ne 0){throw 'Release-start allowed scheduler cleanup'}

# A stub capture emits a drain failure without invoking wsl.exe. The real native
# entrypoint must publish only its outer failure; no restoration FailureHold.
Add-Type -TypeDefinition @'
namespace CreWindowsCapture {
    public class StreamResult { public bool Eof=false; public bool Truncated=false; }
    public class Result {
        public bool Failed=true; public int NativeExitCode=0;
        public StreamResult Stdout=new StreamResult(); public StreamResult Stderr=new StreamResult();
    }
    public static class Capture {
        public static string Marker;
        public static Result Run(string executable, string[] arguments, string stdout, string stderr, string prefix) {
            System.IO.File.WriteAllText(stdout, ""); System.IO.File.WriteAllText(stderr, "");
            var operation=stdout.Substring(0, stdout.Length-".stdout.raw".Length);
            System.IO.Directory.CreateDirectory(operation);
            System.IO.File.WriteAllText(System.IO.Path.Combine(operation,"mutation-start.json"),Marker);
            return new Result();
        }
    }
}
'@
$toolingRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$git=$GitExecutable
$head=(& $git -C $toolingRoot rev-parse HEAD).Trim()
$tree=(& $git -C $toolingRoot rev-parse 'HEAD^{tree}').Trim()
$nativeRelease=@{schema='cre-code-restoration-v1';code=@{sha=$head;tree=$tree};
    roots=@{tooling=$toolingRoot.Replace('\','/')};tooling=@{sha=$head;tree=$tree};task_baseline=$rows;
    programs=@{native_git=$git.Replace('\','/')};wsl=@{distro='offline-fixture';user='offline-fixture'}}
$releasePath=Join-Path $Output 'native-packet.json'
Save-CreHandoffExclusive $releasePath ($nativeRelease|ConvertTo-Json -Depth 15)
$digest=Get-CreHandoffHash ([IO.File]::ReadAllText($releasePath))
$outer=Join-Path $Output 'drain-operation'
$marker.restoration_sha256=$digest;$marker.operation_output=$outer.Replace('\','/');$marker.tooling=$nativeRelease.tooling
[CreWindowsCapture.Capture]::Marker=$marker|ConvertTo-Json -Depth 15
$warnings=@();$script:drainError=$null
& {
    try {& (Join-Path $PSScriptRoot 'Invoke-CreCodeHandoff.ps1') -Mode Apply -RestorationPath $releasePath -RestorationSha $digest -Output $outer -Observation $auth -Authorization $auth 3>&1}
    catch {$script:drainError=$_}
}|ForEach-Object {$warnings+=@($_)}
if(-not $script:drainError -or $script:drainError.Exception.Message -notlike '*exit/drain uncertain*'){throw 'Native drain fixture did not reach the actual outer failure'}
if(@($warnings|Where-Object {$_ -is [Management.Automation.WarningRecord] -and $_.Message -like '*Failure hold uncertain*'}).Count){throw 'Native outer restoration attempted FailureHold'}
if($script:disableCount -ne 0 -or -not (Test-Path -LiteralPath ($outer+'.outer-failure.json'))){throw 'Native outer failure mutated tasks or lost its receipt'}
Save-CreHandoffExclusive (Join-Path $Output 'result.json') (@{schema='cre-native-restoration-fault-proof-v1';
    powershell=$PSVersionTable.PSVersion.ToString();owned_expired_admission_hold=$true;
    release_start_blocks_hold=$true;outer_drain_receipt_only=$true;task_mutations='mocked only';child_dispatches=0}|ConvertTo-Json)
Write-Output 'PASS: owned expiry cleanup; release-start refusal; native outer drain receipt-only'
