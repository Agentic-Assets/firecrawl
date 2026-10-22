<# Foreground code-only route. Preview is offline and is the default.
Observe/Postreadback are read-only. Hold/Publish are internal owned steps.
No task, runtime, install or provider action follows from a prepared release.
#>
[CmdletBinding()]
param(
    [ValidateSet('Preview','Observe','Apply','Postreadback','Hold','Publish')][string]$Mode='Preview',
    [Parameter(Mandatory=$true)][string]$ReleasePath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ReleaseSha,
    [string]$Output,
    [string]$Observation,
    [string]$Authorization,
    [int]$OwnerPid=0
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
$releaseFile=ConvertTo-CreHandoffPath $ReleasePath
$releaseBytes=[IO.File]::ReadAllBytes($releaseFile)
$releaseText=[Text.UTF8Encoding]::new($false,$true).GetString($releaseBytes)
if((Get-CreHandoffHash $releaseText) -cne $ReleaseSha) { throw 'Release input changed' }
$release=$releaseText|ConvertFrom-Json
if($release.schema -cne 'cre-code-release-v1') { throw 'Unknown release schema' }
if($Mode -eq 'Preview') {
    [ordered]@{preview_only=$true;execution=$false;release_sha256=$ReleaseSha;selected=$release.selected}|ConvertTo-Json -Depth 5
    return
}
if(-not $Output) { throw 'Exclusive observation/operation output required' }
$outputRoot=ConvertTo-CreHandoffPath $Output
function ConvertTo-CreWslPath([string]$Path) {
    $native=ConvertTo-CreHandoffPath $Path
    return '/mnt/'+$native.Substring(0,1).ToLowerInvariant()+'/'+$native.Substring(3)
}
$control=(ConvertTo-CreWslPath $release.roots.tooling)+'/scripts/firecrawl-ops/cre_collector/cre_code_handoff.py'
$common=@('-d',[string]$release.wsl.distro,'-u',[string]$release.wsl.user,'--exec','python3','-B',$control,'--release',(ConvertTo-CreWslPath $releaseFile))
function Assert-Input {
    if((Get-CreHandoffHash ([IO.File]::ReadAllText($releaseFile))) -cne $ReleaseSha) { throw 'Release changed at native boundary' }
}
function Assert-Owner {
    Assert-Input
    if($OwnerPid -le 0) { throw 'Exact live maintenance owner required' }
    & wsl.exe @common --assert-owned-pid $OwnerPid
    if($LASTEXITCODE -ne 0) { throw 'Canonical ownership not proven' }
    & wsl.exe @common --check-authorization --observation (ConvertTo-CreWslPath $Observation) --authorization (ConvertTo-CreWslPath $Authorization)
    if($LASTEXITCODE -ne 0) { throw 'Immutable current authorization refused' }
}
if($Mode -eq 'Apply') {
    if(-not $Observation -or -not $Authorization) { throw 'Explicit observation/authorization required' }
    if(-not ('CreWindowsCapture.Capture' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'CreNativeCapture.cs') }
    # The Linux procedure creates its own exclusive operation directory. Native
    # streams are siblings so no pre-created output can masquerade as a retry.
    $result=[CreWindowsCapture.Capture]::Run('wsl.exe',([string[]]($common+@('--apply','--observation',(ConvertTo-CreWslPath $Observation),'--authorization',(ConvertTo-CreWslPath $Authorization),'--output',(ConvertTo-CreWslPath $Output)))),($outputRoot+'.stdout.raw'),($outputRoot+'.stderr.raw'),$null)
    Save-CreHandoffExclusive ($outputRoot+'.native.json') ($result|ConvertTo-Json -Depth 8)
    if($result.Failed -or $result.NativeExitCode -ne 0 -or -not $result.Stdout.Eof -or -not $result.Stderr.Eof) { throw 'Native foreground exit/drain uncertain; inspect canonical recovery and held tasks' }
    return
}
Assert-Input
if($Mode -in @('Hold','Publish')) { Assert-Owner }
# Reject actual changed-path case/reparse drift, accepting equivalent separators.
$native=ConvertTo-CreHandoffPath $release.roots.native
foreach($relative in $release.allowed_paths) {
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
$result=Invoke-CreHandoffTasks $release $Mode $outputRoot ${function:Assert-Owner}
if($Mode -in @('Observe','Postreadback')) {
    Save-CreHandoffExclusive (Join-Path $outputRoot 'native-observation.json') ($result|ConvertTo-Json -Depth 15)
} else {
    Save-CreHandoffExclusive (Join-Path $outputRoot ('native-'+$Mode.ToLowerInvariant()+'.json')) (@{status='passed';mode=$Mode;release_sha256=$ReleaseSha;owner_pid=$OwnerPid;completed_at=[DateTime]::UtcNow.ToString('o')}|ConvertTo-Json)
}
