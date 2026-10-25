<# Foreground code-only route. Preview is offline and is the default.
Observe/Postreadback are read-only. Hold/Publish are internal owned steps.
No task, runtime, install or provider action follows from a prepared release.
#>
[CmdletBinding(DefaultParameterSetName='Release')]
param(
    [ValidateSet('Preview','Observe','Apply','Postreadback','Verify','Hold','Repin','Restore','FailureHold')][string]$Mode='Preview',
    [Parameter(Mandatory=$true,ParameterSetName='Release')][string]$ReleasePath,
    [Parameter(Mandatory=$true,ParameterSetName='Release')][ValidatePattern('^[0-9a-f]{64}$')][string]$ReleaseSha,
    [Parameter(Mandatory=$true,ParameterSetName='Restoration')][string]$RestorationPath,
    [Parameter(Mandatory=$true,ParameterSetName='Restoration')][ValidatePattern('^[0-9a-f]{64}$')][string]$RestorationSha,
    [string]$Output,
    [string]$Observation,
    [string]$Authorization,
    [string]$OperationOutput,
    [int]$OwnerPid=0
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
$restoration=$PSCmdlet.ParameterSetName -ceq 'Restoration'
if($restoration) {$ReleasePath=$RestorationPath;$ReleaseSha=$RestorationSha}
$digestField=if($restoration){'restoration_sha256'}else{'release_sha256'}
$releaseFile=ConvertTo-CreHandoffPath $ReleasePath
$releaseBytes=[IO.File]::ReadAllBytes($releaseFile)
$releaseText=[Text.UTF8Encoding]::new($false,$true).GetString($releaseBytes)
if((Get-CreHandoffHash $releaseText) -cne $ReleaseSha) { throw 'Release input changed' }
$release=Read-CreHandoffJson $releaseText
if($release.schema -cne $(if($restoration){'cre-code-restoration-v1'}else{'cre-code-release-v1'})) { throw 'Input selector/schema differs' }
if($restoration -and $Mode -in @('Hold','Repin')) {throw 'Restoration cannot hold/repin/register tasks'}
$actualSourceRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
if((ConvertTo-CreHandoffPath $actualSourceRoot) -cne (ConvertTo-CreHandoffPath $release.roots.tooling)) { throw 'Invoked native source root differs from declared tooling' }
if($Mode -eq 'Preview') {
    $preview=[ordered]@{preview_only=$true;execution=$false}
    $preview[$digestField]=$ReleaseSha
    $preview[$(if($restoration){'code'}else{'selected'})]=if($restoration){$release.code}else{$release.selected}
    $preview|ConvertTo-Json -Depth 5
    return
}
Assert-CreHandoffTooling $release $actualSourceRoot
if(-not $Output) { throw 'Exclusive observation/operation output required' }
$outputRoot=ConvertTo-CreHandoffPath $Output
function ConvertTo-CreWslPath([string]$Path) {
    $native=ConvertTo-CreHandoffPath $Path
    return '/mnt/'+$native.Substring(0,1).ToLowerInvariant()+'/'+$native.Substring(3)
}
$control=(ConvertTo-CreWslPath $release.roots.tooling)+'/scripts/firecrawl-ops/cre_collector/cre_code_handoff.py'
$selector=if($restoration){'--restoration-packet'}else{'--release'}
$common=@('-d',[string]$release.wsl.distro,'-u',[string]$release.wsl.user,'--exec','python3','-B',$control,$selector,(ConvertTo-CreWslPath $releaseFile))
function Assert-Input {
    if((Get-CreHandoffHash ([IO.File]::ReadAllText($releaseFile))) -cne $ReleaseSha) { throw 'Release changed at native boundary' }
}
function Assert-LeaseOwner {
    Assert-Input
    if($OwnerPid -le 0) { throw 'Exact live maintenance owner required' }
    $ownedInputs=if($restoration){@('--output',(ConvertTo-CreWslPath $Output),'--observation',(ConvertTo-CreWslPath $Observation),'--authorization',(ConvertTo-CreWslPath $Authorization))}else{@()}
    & wsl.exe @common --assert-owned-pid $OwnerPid @ownedInputs
    if($LASTEXITCODE -ne 0) { throw 'Canonical ownership not proven' }
}
function Assert-Owner {
    Assert-LeaseOwner
    $authorizationOutput=if($restoration){@('--output',(ConvertTo-CreWslPath $Output))}else{@()}
    & wsl.exe @common --check-authorization --observation (ConvertTo-CreWslPath $Observation) --authorization (ConvertTo-CreWslPath $Authorization) @authorizationOutput
    if($LASTEXITCODE -ne 0) { throw 'Immutable current authorization refused' }
}
if($Mode -eq 'Apply') {
    if(-not $Observation -or -not $Authorization) { throw 'Explicit observation/authorization required' }
    foreach($existing in @($outputRoot,($outputRoot+'.stdout.raw'),($outputRoot+'.stderr.raw'),($outputRoot+'.native.json'),($outputRoot+'.outer-failure.json'))) {
        if(Test-Path -LiteralPath $existing) { throw 'Exclusive operation output already exists; no replay or cleanup of a prior attempt' }
    }
    if(-not ('CreWindowsCapture.Capture' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'CreNativeCapture.cs') }
    # The Linux procedure creates its own exclusive operation directory. Native
    # streams are siblings so no pre-created output can masquerade as a retry.
    try {
        $result=[CreWindowsCapture.Capture]::Run('wsl.exe',([string[]]($common+@('--apply','--observation',(ConvertTo-CreWslPath $Observation),'--authorization',(ConvertTo-CreWslPath $Authorization),'--output',(ConvertTo-CreWslPath $Output)))),($outputRoot+'.stdout.raw'),($outputRoot+'.stderr.raw'),$null)
        if($result.Failed -or $result.NativeExitCode -ne 0 -or -not $result.Stdout.Eof -or -not $result.Stderr.Eof -or $result.Stdout.Truncated -or $result.Stderr.Truncated) { throw 'Native foreground exit/drain uncertain' }
        $completion=[ordered]@{schema=$(if($restoration){'cre-code-restoration-native-completion-v1'}else{'cre-code-native-completion-v1'});operation_output=$outputRoot;capture=$result;
            authorization_sha256=(Get-CreHandoffHash ([IO.File]::ReadAllText((ConvertTo-CreHandoffPath $Authorization))));
            stdout_sha256=(Get-FileHash -LiteralPath ($outputRoot+'.stdout.raw') -Algorithm SHA256).Hash.ToLowerInvariant();
            stderr_sha256=(Get-FileHash -LiteralPath ($outputRoot+'.stderr.raw') -Algorithm SHA256).Hash.ToLowerInvariant()}
        $completion[$digestField]=$ReleaseSha
        Save-CreHandoffExclusive ($outputRoot+'.native.json') ($completion|ConvertTo-Json -Depth 10)
    } catch {
        $original=$_
        if(-not $restoration) {
            try {$null=Invoke-CreHandoffFailureHold $release $outputRoot $ReleaseSha $Authorization 'outer-failure-hold.json'} catch {Write-Warning ('Failure hold uncertain: '+$_.Exception.Message)}
        }
        try {
            $failure=@{schema=$(if($restoration){'cre-code-restoration-outer-failure-v1'}else{'cre-code-outer-failure-v1'});
                original_error_type=$original.Exception.GetType().FullName;original_error=$original.Exception.Message;
                operation_output=$outputRoot;commit_and_recovery_state='unknown; independent readback required'}
            $failure[$digestField]=$ReleaseSha
            Save-CreHandoffExclusive ($outputRoot+'.outer-failure.json') ($failure|ConvertTo-Json)
        } catch {Write-Warning ('Original failure: '+$original.Exception.Message+'; outer failure receipt unavailable: '+$_.Exception.GetType().FullName)}
        Write-Warning 'Postcommit confirmation may be unknown; recovery retention is not asserted. Independent readback required.'
        throw $original
    }
    return
}
Assert-Input
if($Mode -eq 'FailureHold') {
    $null=Invoke-CreHandoffFailureHold $release $outputRoot $ReleaseSha $Authorization -AssertOwner ${function:Assert-LeaseOwner}
    return
}
if($Mode -in @('Hold','Repin','Restore','Verify')) { Assert-Owner }
# Reject actual changed-path case/reparse drift, accepting equivalent separators.
$native=ConvertTo-CreHandoffPath $release.roots.native
foreach($relative in $(if($restoration){@()}else{$release.allowed_paths})) {
    $current=$native
    foreach($part in ($relative -split '/')) {
        if(Test-Path -LiteralPath $current -PathType Container) {
            foreach($item in @(Get-ChildItem -LiteralPath $current -Force|Where-Object {$_.Name -ieq $part})) {
                if($item.Name -cne $part -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Changed-path case/reparse collision' }
            }
        }
        $current=Join-Path $current $part
    }
}
$operation=$null
if($Mode -eq 'Hold' -or ($restoration -and $Mode -eq 'Restore')) {
    $operation=[ordered]@{schema=$(if($restoration){'cre-code-restoration-mutation-v1'}else{'cre-code-mutation-v1'});
        authorization_sha256=(Get-CreHandoffHash ([IO.File]::ReadAllText((ConvertTo-CreHandoffPath $Authorization))));
        operation_output=$outputRoot;tooling=$release.tooling;fleet=@($release.task_baseline);entered_at=[DateTime]::UtcNow.ToString('o')}
    $operation[$digestField]=$ReleaseSha
}
$result=Invoke-CreHandoffTasks $release $Mode $outputRoot ${function:Assert-Owner} $operation $OperationOutput
if($Mode -in @('Observe','Postreadback','Verify')) {
    Save-CreHandoffExclusive (Join-Path $outputRoot 'native-observation.json') ($result|ConvertTo-Json -Depth 15)
} else {
    $receipt=@{status='passed';mode=$Mode;owner_pid=$OwnerPid;completed_at=[DateTime]::UtcNow.ToString('o')}
    $receipt[$digestField]=$ReleaseSha
    Save-CreHandoffExclusive (Join-Path $outputRoot ('native-'+$Mode.ToLowerInvariant()+'.json')) ($receipt|ConvertTo-Json)
}
