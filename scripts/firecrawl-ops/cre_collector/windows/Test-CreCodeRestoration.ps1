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
