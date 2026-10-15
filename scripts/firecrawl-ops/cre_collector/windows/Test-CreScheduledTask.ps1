<#
.SYNOPSIS
Exercise task definition/registration boundaries; optionally run an owned native fixture.
.DESCRIPTION
Default tests mock all Task Scheduler mutations and never invoke CRE or WSL.
-NativeFixture additionally registers one GUID-named, disabled, triggerless
synthetic task in the root scheduler folder, runs a bounded local script, checks
exit/IgnoreNew behavior, disables it and removes only that exact owned fixture.
#>
[CmdletBinding()]
param([string]$ProofDirectory, [switch]$NativeFixture)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows native tests only.' }
if (-not $ProofDirectory) {
    $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/cre-scheduled-task-' + [Guid]::NewGuid().ToString('N'))
}
[void](New-Item -ItemType Directory -Path $ProofDirectory -Force)
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$creInstaller = Join-Path $PSScriptRoot 'Install-CreScheduledTask.ps1'
$creBase = @{ TaskName='Offline Fixture'; Tier='monitor'; Source='hanley'; ExpectedSha=('a' * 40) }
$global:Cre4265Fixture = [pscustomobject]@{ Checks=0; Calls=$null; Xml=""; Existing=$false; Tamper=$false; EnumerationFails=$false; PostEnableFailure=$false }
$global:Cre4265Fixture.Calls = New-Object 'System.Collections.Generic.List[string]'
$global:Cre4265Fixture.Xml = ''
$global:Cre4265Fixture.Existing = $false
$global:Cre4265Fixture.Tamper = $false
$global:Cre4265Fixture.EnumerationFails = $false
function Assert-Cre($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $global:Cre4265Fixture.Checks++
}
function Get-CreXmlValue($Doc, [string]$Path) {
    $ns = New-Object Xml.XmlNamespaceManager($Doc.NameTable)
    $ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    return $Doc.SelectSingleNode($Path, $ns).InnerText
}
# Functions shadow cmdlets only inside this test script's scope.
function Get-ScheduledTask {
    param($ErrorAction)
    $global:Cre4265Fixture.Calls.Add('get')
    if ($global:Cre4265Fixture.EnumerationFails) { throw 'synthetic enumeration denied' }
    if ($global:Cre4265Fixture.Existing) { [pscustomobject]@{ TaskName='Offline Fixture'; TaskPath='\Agentic Assets\CRE\' } }
}
$creFolder = [pscustomobject]@{}
$creFolder | Add-Member -MemberType ScriptMethod -Name GetFolder -Value { param($Path) return $this }
$creFolder | Add-Member -MemberType ScriptMethod -Name RegisterTask -Value {
    param($Name,$Xml,$Flags,$UserSid,$Password,$LogonType,$Sddl)
    $global:Cre4265Fixture.Calls.Add('register')
    Assert-Cre ($Name -ceq 'Offline Fixture' -and $Flags -eq 2 -and $LogonType -eq 3 -and $null -eq $Password) 'Registration must atomically create only, with the interactive current user and no password.'
    Assert-Cre ($UserSid -ceq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) 'Registration must bind the current SID.'
    $global:Cre4265Fixture.Xml = $Xml
}
$creService = [pscustomobject]@{ Folder=$creFolder }
$creService | Add-Member -MemberType ScriptMethod -Name Connect -Value { $global:Cre4265Fixture.Calls.Add('connect') }
$creService | Add-Member -MemberType ScriptMethod -Name GetFolder -Value { param($Path) return $this.Folder }
$global:Cre4265Fixture | Add-Member -NotePropertyName Service -NotePropertyValue $creService
function New-Object {
    [CmdletBinding(DefaultParameterSetName='Type')]
    param(
        [Parameter(Position=0,ParameterSetName='Type')][string]$TypeName,
        [Parameter(Position=1,ParameterSetName='Type')][object[]]$ArgumentList,
        [Parameter(Mandatory=$true,ParameterSetName='Com')][string]$ComObject
    )
    if ($PSCmdlet.ParameterSetName -ceq 'Com') {
        Assert-Cre ($ComObject -ceq 'Schedule.Service') 'Only Task Scheduler COM may be constructed.'
        return $global:Cre4265Fixture.Service
    }
    return Microsoft.PowerShell.Utility\New-Object @PSBoundParameters
}
function Export-ScheduledTask {
    param($TaskName,$TaskPath,$ErrorAction)
    $global:Cre4265Fixture.Calls.Add('export')
    if ($global:Cre4265Fixture.PostEnableFailure -and $global:Cre4265Fixture.Calls.Contains('enable')) { throw 'synthetic enabled readback denied' }
    if ($global:Cre4265Fixture.Tamper) { return $global:Cre4265Fixture.Xml.Replace('<WakeToRun>false</WakeToRun>','<WakeToRun>true</WakeToRun>') }
    return $global:Cre4265Fixture.Xml
}
function Enable-ScheduledTask {
    param($TaskName,$TaskPath,$ErrorAction)
    $global:Cre4265Fixture.Calls.Add('enable')
    $global:Cre4265Fixture.Xml = $global:Cre4265Fixture.Xml.Replace('<Enabled>false</Enabled><Hidden>','<Enabled>true</Enabled><Hidden>')
}
function Disable-ScheduledTask {
    param($TaskName,$TaskPath,$ErrorAction)
    $global:Cre4265Fixture.Calls.Add('disable')
    $global:Cre4265Fixture.Xml = $global:Cre4265Fixture.Xml.Replace('<Enabled>true</Enabled><Hidden>','<Enabled>false</Enabled><Hidden>')
}
$crePreview = (& $creInstaller @creBase) -join [Environment]::NewLine
Assert-Cre ($global:Cre4265Fixture.Calls.Count -eq 0) 'Preview must not even enumerate scheduler tasks.'
[xml]$creDoc = $crePreview
Assert-Cre ((Get-CreXmlValue $creDoc '/t:Task/t:Settings/t:Enabled') -ceq 'false') 'Preview must be disabled.'
$creSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Assert-Cre ((Get-CreXmlValue $creDoc '/t:Task/t:Principals/t:Principal/t:UserId') -ceq $creSid) 'Task must use the current SID.'
foreach ($crePair in @(
    @('LogonType','InteractiveToken'),@('RunLevel','LeastPrivilege')
)) {
    Assert-Cre ((Get-CreXmlValue $creDoc ('/t:Task/t:Principals/t:Principal/t:'+$crePair[0])) -ceq $crePair[1]) ('Principal contract: '+$crePair[0])
}
foreach ($crePair in @(
    @('MultipleInstancesPolicy','IgnoreNew'),@('AllowHardTerminate','false'),
    @('StartWhenAvailable','true'),@('WakeToRun','false'),@('Hidden','false'),
    @('ExecutionTimeLimit','PT0S'),@('RunOnlyIfIdle','false'),
    @('StopIfGoingOnBatteries','false')
)) {
    Assert-Cre ((Get-CreXmlValue $creDoc ('/t:Task/t:Settings/t:'+$crePair[0])) -ceq $crePair[1]) ('Settings contract: '+$crePair[0])
}
Assert-Cre ($crePreview -match '08:00:00' -and $crePreview -match '20:00:00' -and $crePreview -notmatch '<LogonTrigger') 'Preview must retain the two explicit daily times, with no implicit trigger.'
$creAction = Get-CreXmlValue $creDoc '/t:Task/t:Actions/t:Exec/t:Arguments'
Assert-Cre ($creAction -match '"-File" "' -and $creAction -notmatch '-Command|-EncodedCommand' -and $creAction -notmatch '"-Apply"') 'Default fixed action must preserve database-free monitor semantics.'
# Preview must preserve a private path without opening it or reading a secret.
$creNotificationPath = "C:\private canary & owner's\not-provisioned.url"
$creNotificationArgs = $creBase.Clone()
$creNotificationArgs['AlertWebhookFile'] = $creNotificationPath
$creNotificationArgs['NotificationMode'] = 'StartFinish'
[xml]$creNotificationDoc = (& $creInstaller @creNotificationArgs) -join [Environment]::NewLine
$creNotificationAction = Get-CreXmlValue $creNotificationDoc '/t:Task/t:Actions/t:Exec/t:Arguments'
Assert-Cre ($creNotificationAction.EndsWith(('"-AlertWebhookFile" "'+$creNotificationPath+'" "-NotificationMode" "StartFinish"'))) 'Private reference and lifecycle mode must survive XML/argument serialization exactly.'
Assert-Cre ($global:Cre4265Fixture.Calls.Count -eq 0 -and $creNotificationAction -notmatch 'https://') 'Notification preview must not read secrets or enumerate tasks.'
$creNotificationRefused=$false
try { & $creInstaller @creNotificationArgs -Register | Out-Null } catch { $creNotificationRefused=$true }
Assert-Cre ($creNotificationRefused -and $global:Cre4265Fixture.Calls.Count -eq 0) 'A missing StartFinish credential must fail before registration or scheduler enumeration.'
$creSecretDirectory=Join-Path ([IO.Path]::GetTempPath()) ('cre-task-config-'+[Guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $creSecretDirectory)
$creSecretFile=Join-Path $creSecretDirectory 'synthetic.url'
[IO.File]::WriteAllText($creSecretFile,'https://example.invalid/SECRET_CANARY',(New-Object Text.UTF8Encoding($false)))
$creSecretAcl=Microsoft.PowerShell.Utility\New-Object Security.AccessControl.FileSecurity
$creSecretAcl.SetSecurityDescriptorSddlForm("O:${creSid}D:P(A;;FA;;;${creSid})(A;;FA;;;SY)(A;;FA;;;BA)")
Set-Acl -LiteralPath $creSecretFile -AclObject $creSecretAcl
$creValidNotificationArgs=$creNotificationArgs.Clone()
$creValidNotificationArgs['AlertWebhookFile']=$creSecretFile
$creValidNotificationResult=(& $creInstaller @creValidNotificationArgs -Register) -join [Environment]::NewLine | ConvertFrom-Json
Assert-Cre (-not $creValidNotificationResult.enabled -and $global:Cre4265Fixture.Calls.Contains('register')) 'A valid private StartFinish file must allow only the existing mocked disabled registration.'
Assert-Cre (-not $global:Cre4265Fixture.Xml.Contains('SECRET_CANARY')) 'Even validated credentials must never enter task XML.'
$global:Cre4265Fixture.Calls.Clear()
# Exercise actual Windows argv decoding rather than only matching XML text.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CreTaskArguments {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CommandLineToArgvW(string commandLine, out int count);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
    public static string[] Decode(string value) {
        int count; IntPtr block = CommandLineToArgvW("fixture.exe " + value, out count);
        if (block == IntPtr.Zero) throw new InvalidOperationException();
        try {
            string[] result = new string[count-1];
            for (int i=1; i<count; i++) result[i-1] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(block, i*IntPtr.Size));
            return result;
        } finally { LocalFree(block); }
    }
}
'@
$creDecoded = [CreTaskArguments]::Decode($creNotificationAction)
$creWebhookIndex = [Array]::IndexOf($creDecoded,'-AlertWebhookFile')
Assert-Cre ($creWebhookIndex -ge 0 -and $creDecoded[$creWebhookIndex+1] -ceq $creNotificationPath) 'Windows argv decoding must preserve the private path including spaces, apostrophe and ampersand.'
Assert-Cre ($creDecoded[$creWebhookIndex+2] -ceq '-NotificationMode' -and $creDecoded[$creWebhookIndex+3] -ceq 'StartFinish') 'Windows argv decoding must preserve the explicit notification mode.'
$creTokens=$null; $creErrors=$null
$creInstallerAst=[Management.Automation.Language.Parser]::ParseFile($creInstaller,[ref]$creTokens,[ref]$creErrors)
$creContractFunction=@($creInstallerAst.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Get-CreTaskContract'},$true))[0]
. ([scriptblock]::Create($creContractFunction.Extent.Text))
$creEnabledXml=$crePreview.Replace('<Enabled>false</Enabled><Hidden>','<Enabled>true</Enabled><Hidden>')
$creDefaultEnabledXml=$creEnabledXml.Replace('<Enabled>true</Enabled><Hidden>','<Hidden>')
Assert-Cre ((Get-CreTaskContract $creEnabledXml) -ceq (Get-CreTaskContract $creDefaultEnabledXml)) 'Native omission of Settings/Enabled must equal its documented true default.'
Assert-Cre ((Get-CreTaskContract $crePreview) -cne (Get-CreTaskContract $creDefaultEnabledXml)) 'Explicit Settings/Enabled=false must remain distinct from omitted/default true.'
$creSeries = @{ TaskName='Offline Fixture'; Tier='series'; Source='interra-realty'; ExpectedSha=('a'*40); WorkDeadlineSeconds=3600; AcknowledgeDatabaseWrites=$true; AdmitCurrentInventorySource='interra-realty'; DailyAt='03:15'; WeeklyOn='Saturday' }
$creSeriesPreview = (& $creInstaller @creSeries) -join [Environment]::NewLine
Assert-Cre ($creSeriesPreview -match '<Saturday\s*/>' -and $creSeriesPreview -match '03:15:00' -and $creSeriesPreview -match 'AcknowledgeDatabaseWrites' -and $creSeriesPreview -match 'AdmitCurrentInventorySource') 'Weekly Interra series must preserve explicit work and admission flags.'
$creInvalid = @(
    @{ NotificationMode='invalid' }, @{ NotificationMode='StartFinish' },
    @{ AlertWebhookFile='relative.url' }, @{ AlertWebhookFile='https://example.invalid/canary' },
    @{ AlertWebhookFile='\\server\share\hook.url' }, @{ AlertWebhookFile='C:\private\hook.url:stream' },
    @{ AlertWebhookFile=('C:\private\hook.url'+[char]10) },
    @{ Enable=$true }, @{ Source='unknown-source' }, @{ Tier='monitor'; Source='all' },
    @{ Source='jll' }, @{ Source='jll-investor' }, @{ Source='colliers' }, @{ Source='cbre-dealflow' },
    @{ ExpectedSha='main' }, @{ ExpectedSha=('A'*40) }, @{ ExpectedSha=(('a'*40)+[char]10) }, @{ Batch=0 },
    @{ DeadlineSeconds=14401 }, @{ TaskName='bad\name' }, @{ TaskName='bad"name' },
    @{ DailyAt='08:00,08:00' }, @{ DailyAt='24:00' }, @{ DailyAt='08:00;bad' }, @{ DailyAt=('08:00'+[char]10) },
    @{ Tier='series'; Source='all' }, @{ WorkDeadlineSeconds=30 },
    @{ AcknowledgeDatabaseWrites=$true }, @{ AdmitCurrentInventorySource='interra-realty' },
    @{ Tier='enrich'; Source='matthews'; Apply=$true; DeadlineSeconds=601 }
)
foreach ($creChange in $creInvalid) {
    $creArgs = $creBase.Clone()
    foreach ($creKey in $creChange.Keys) { $creArgs[$creKey] = $creChange[$creKey] }
    $global:Cre4265Fixture.Calls.Clear()
    $creRefused = $false
    try { & $creInstaller @creArgs | Out-Null } catch { $creRefused=$true }
    Assert-Cre ($creRefused -and $global:Cre4265Fixture.Calls.Count -eq 0) ('Invalid input must refuse before any scheduler operation: '+($creChange | ConvertTo-Json -Compress))
}
foreach ($creChange in @(@{Apply=$true},@{Batch=2},@{DeadlineSeconds=30})) {
    $creArgs=$creSeries.Clone()
    foreach ($creKey in $creChange.Keys) { $creArgs[$creKey]=$creChange[$creKey] }
    $global:Cre4265Fixture.Calls.Clear(); $creRefused=$false
    try { & $creInstaller @creArgs | Out-Null } catch { $creRefused=$true }
    Assert-Cre ($creRefused -and $global:Cre4265Fixture.Calls.Count -eq 0) 'Series must reject explicit incremental options.'
}
$global:Cre4265Fixture.Calls.Clear()
$creInstallResult = (& $creInstaller @creBase -Register) -join [Environment]::NewLine | ConvertFrom-Json
Assert-Cre (-not $creInstallResult.enabled -and -not $creInstallResult.pipelineRunVerified -and ($global:Cre4265Fixture.Calls -join ',') -ceq 'get,connect,register,export') 'Register must remain disabled and verify its actual definition.'
$global:Cre4265Fixture.Calls.Clear()
$creInstallResult = (& $creInstaller @creBase -Register -Enable) -join [Environment]::NewLine | ConvertFrom-Json
Assert-Cre ($creInstallResult.enabled -and ($global:Cre4265Fixture.Calls -join ',') -ceq 'get,connect,register,export,enable,export') 'Explicit enable must follow disabled registration and exact readback.'
foreach ($creCase in @('collision','enumeration-failure','drift','enabled-readback-error')) {
    $global:Cre4265Fixture.Calls.Clear()
    $global:Cre4265Fixture.Existing=$creCase -ceq 'collision'
    $global:Cre4265Fixture.EnumerationFails=$creCase -ceq 'enumeration-failure'
    $global:Cre4265Fixture.Tamper=$creCase -ceq 'drift'
    $global:Cre4265Fixture.PostEnableFailure=$creCase -ceq 'enabled-readback-error'
    $creRefused=$false
    try { & $creInstaller @creBase -Register -Enable | Out-Null } catch { $creRefused=$true }
    if ($creCase -ceq 'enabled-readback-error') {
        Assert-Cre ($creRefused -and $global:Cre4265Fixture.Calls.Contains('disable') -and $global:Cre4265Fixture.Xml.Contains('<Enabled>false</Enabled><Hidden>')) 'Failed post-enable readback must disable the newly created task.'
    } else {
        Assert-Cre ($creRefused -and -not $global:Cre4265Fixture.Calls.Contains('enable')) ('Registration/enable must refuse: '+$creCase)
        if ($creCase -cne 'drift') { Assert-Cre (-not $global:Cre4265Fixture.Calls.Contains('register')) 'Enumeration failure/collision must not register.' }
    }
}
$global:Cre4265Fixture.Existing=$false; $global:Cre4265Fixture.EnumerationFails=$false; $global:Cre4265Fixture.Tamper=$false; $global:Cre4265Fixture.PostEnableFailure=$false
# Dot-source preview only to exercise the shipped argument encoder/contract.
$null = . $creInstaller @creBase
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CreScheduledArgv {
    [DllImport("shell32.dll", SetLastError=true)]
    static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)]string command, out int count);
    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr handle);
    public static string[] Parse(string command) {
        int count; var ptr=CommandLineToArgvW(command,out count);
        if(ptr==IntPtr.Zero) throw new InvalidOperationException("argv parse failed");
        try { var result=new string[count]; for(int i=0;i<count;i++) result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(ptr,i*IntPtr.Size)); return result; }
        finally { LocalFree(ptr); }
    }
}
'@
foreach ($creValue in @('', 'two words','a"b','C:\space path\','a\"b','&|<>^%PATH%$(literal);','café', ('x' + [char]0x2603))) {
    $creParsed = [CreScheduledArgv]::Parse('"fixture.exe" '+(ConvertTo-CreNativeArgument $creValue))
    Assert-Cre ($creParsed.Count -eq 2 -and $creParsed[1] -ceq $creValue) 'Native Windows argv must preserve literal bytes.'
}
$creDecoded = [CreScheduledArgv]::Parse('"fixture.exe" '+$creAction)
$creLauncher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
Assert-Cre (($creDecoded -join '|') -ceq ('fixture.exe|-NoProfile|-NonInteractive|-File|'+$creLauncher+'|-Tier|monitor|-Source|hanley|-ExpectedSha|'+('a'*40)+'|-Batch|50|-DeadlineSeconds|1800')) 'Decode the actual serialized action, including the absolute fixed launcher.'
$creEquivalent = $crePreview.Replace('<RegistrationInfo>','<RegistrationInfo><Date>2026-01-01T00:00:00</Date><URI>\fixture</URI>')
Assert-Cre ((Get-CreTaskContract $creEquivalent) -ceq (Get-CreTaskContract $crePreview)) 'Scheduler registration metadata must not masquerade as execution drift.'
Assert-Cre ((Get-CreTaskContract $crePreview.Replace('<WakeToRun>false','<WakeToRun>true')) -cne (Get-CreTaskContract $crePreview)) 'Execution settings drift must be detected.'
$creOffline = [ordered]@{ status='passed'; assertions=$global:Cre4265Fixture.Checks; nativeFixtureRequested=[bool]$NativeFixture; productionTaskRegistered=$false; providerOrDatabaseCalled=$false }
$creOffline | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ProofDirectory 'offline.json') -Encoding UTF8
$pipelineTaskArgs = $creBase.Clone()
$pipelineTaskArgs.Remove('Source')
$pipelineTaskArgs['Tier'] = 'series'
$pipelineTaskArgs['Sources'] = 'interra-realty,faris-lee'
$pipelineTaskArgs['Pipeline'] = $true
$pipelineTaskArgs['AdmitCurrentInventorySources'] = 'interra-realty,faris-lee'
$pipelineTaskArgs['WorkDeadlineSeconds'] = 3600
$pipelineTaskArgs['AcknowledgeDatabaseWrites'] = $true
foreach ($workers in @(1,2,3,4)) {
    $pipelineTaskArgs['SourceWorkers'] = $workers
    [xml]$pipelineDoc = (& $creInstaller @pipelineTaskArgs) -join [Environment]::NewLine
    $pipelineAction = Get-CreXmlValue $pipelineDoc '/t:Task/t:Actions/t:Exec/t:Arguments'
    Assert-Cre ($pipelineAction.Contains('"-Sources" "interra-realty,faris-lee"') -and $pipelineAction.Contains(('"-Pipeline" "-SourceWorkers" "'+$workers+'"')) -and $pipelineAction.Contains('"-AdmitCurrentInventorySources" "interra-realty,faris-lee"')) 'Preview forwards the selected worker limit and finite admissions without creating a task.'
}
$pipelineTaskArgs.Remove('SourceWorkers')
[xml]$pipelineDoc = (& $creInstaller @pipelineTaskArgs) -join [Environment]::NewLine
Assert-Cre ((Get-CreXmlValue $pipelineDoc '/t:Task/t:Actions/t:Exec/t:Arguments').Contains('"-SourceWorkers" "2"')) 'Omitted opt-in pipeline setting resolves two in the visible preview.'
$crePreview | Set-Content -LiteralPath (Join-Path $ProofDirectory 'preview.xml') -Encoding UTF8
Write-Output ('Offline scheduled-task PASS: '+$global:Cre4265Fixture.Checks+' assertions')

if (-not $NativeFixture) { return }
# Remove only this script's mocks before the explicitly requested native phase.
foreach ($creName in @('Get-ScheduledTask','New-Object','Export-ScheduledTask','Enable-ScheduledTask','Disable-ScheduledTask')) {
    Remove-Item -LiteralPath ('Function:\'+$creName)
}
$creFixtureId = [Guid]::NewGuid().ToString('N')
$creFixtureName = 'CRE4265-Fixture-'+$creFixtureId
$creFixtureFile = Join-Path $ProofDirectory ('fixture-'+$creFixtureId+'.ps1')
$creFixtureLog = Join-Path $ProofDirectory ('fixture-'+$creFixtureId+'.started')
$creFixtureText = "[IO.File]::AppendAllText('"+$creFixtureLog.Replace("'","''")+"', 'started'+[Environment]::NewLine); Start-Sleep -Seconds 8; exit 23"
[IO.File]::WriteAllText($creFixtureFile,$creFixtureText,(New-Object Text.UTF8Encoding($true)))
[xml]$creFixtureDoc = $crePreview
$creNs = New-Object Xml.XmlNamespaceManager($creFixtureDoc.NameTable)
$creNs.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
$creFixtureDoc.SelectSingleNode('/t:Task/t:Triggers',$creNs).RemoveAll()
$creFixtureDoc.SelectSingleNode('/t:Task/t:RegistrationInfo/t:Description',$creNs).InnerText='Private synthetic CRE4265 fixture '+$creFixtureId
$creFixtureDoc.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments',$creNs).InnerText=(@('-NoProfile','-NonInteractive','-File',$creFixtureFile) | ForEach-Object { ConvertTo-CreNativeArgument $_ }) -join ' '
$creFixtureDoc.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:WorkingDirectory',$creNs).InnerText=$ProofDirectory
$creRegistered=$false
$creNativeResult=[ordered]@{ taskName=$creFixtureName; taskPath='\'; status='pending'; cleanup='not_registered'; providerOrDatabaseCalled=$false; productionTaskRegistered=$false }
try {
    if (@(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -ieq $creFixtureName }).Count) { throw 'Unexpected fixture-name collision.' }
    Register-CreTaskCreateOnly $creFixtureName '\' $creFixtureDoc.OuterXml $creSid
    $creRegistered=$true
    $creActual=Export-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop
    $creActual | Set-Content -LiteralPath (Join-Path $ProofDirectory 'native-disabled.xml') -Encoding UTF8
    Assert-Cre ((Get-CreTaskContract $creActual) -ceq (Get-CreTaskContract $creFixtureDoc.OuterXml)) 'Native serialized definition must match the reviewed disabled fixture.'
    Assert-Cre ((Get-ScheduledTask -TaskName $creFixtureName -TaskPath '\').State -eq 'Disabled') 'Native fixture must really register disabled.'
    $creCollisionRefused=$false
    try { Register-CreTaskCreateOnly $creFixtureName '\' $creFixtureDoc.OuterXml $creSid } catch { $creCollisionRefused=$true }
    Assert-Cre $creCollisionRefused 'Native atomic create must refuse an existing task, without replacement.'
    Assert-Cre ((Get-CreTaskContract (Export-ScheduledTask -TaskName $creFixtureName -TaskPath '\')) -ceq (Get-CreTaskContract $creActual)) 'Atomic collision must leave the fixture definition unchanged.'
    $creNativeService=New-Object -ComObject 'Schedule.Service'
    $creNativeService.Connect()
    $creNativeDefinition=$creNativeService.NewTask(0)
    $creNativeDefinition.XmlText=$crePreview
    Assert-Cre ((Get-CreTaskContract $creNativeDefinition.XmlText) -ceq (Get-CreTaskContract $crePreview)) 'Native scheduler definition must preserve the real calendar triggers before registration.'
    [void](Enable-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop)
    [void](Start-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop)
    $creWait=[Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath $creFixtureLog) -and $creWait.Elapsed.TotalSeconds -lt 20) { Start-Sleep -Milliseconds 100 }
    Assert-Cre (Test-Path -LiteralPath $creFixtureLog) 'Native synthetic action must actually start.'
    [void](Start-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop)
    while ((Get-ScheduledTask -TaskName $creFixtureName -TaskPath '\').State -eq 'Running' -and $creWait.Elapsed.TotalSeconds -lt 35) { Start-Sleep -Milliseconds 200 }
    Assert-Cre ((Get-ScheduledTask -TaskName $creFixtureName -TaskPath '\').State -ne 'Running') 'Finite synthetic action must settle.'
    $creInfo=Get-ScheduledTaskInfo -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop
    Assert-Cre ($creInfo.LastTaskResult -eq 23) 'Native task must retain the actual synthetic exit code.'
    Assert-Cre (@([IO.File]::ReadAllLines($creFixtureLog)).Count -eq 1) 'IgnoreNew must prevent a second action from starting.'
    [void](Disable-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop)
    Assert-Cre ((Get-ScheduledTask -TaskName $creFixtureName -TaskPath '\').State -eq 'Disabled') 'Native fixture must disable again.'
    $creNativeResult.status='passed'
    $creNativeResult.lastTaskResult=[int]$creInfo.LastTaskResult
    $creNativeResult.starts=1
    $creNativeResult.assertions=$global:Cre4265Fixture.Checks
} catch {
    $creNativeResult.status='failed'
    $creNativeResult.error=$_.ToString()
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $ProofDirectory 'native-error.txt') -Encoding UTF8
    throw
} finally {
    if ($creRegistered) {
        # Compare our own unique marker/action/SID before any cleanup mutation.
        [xml]$creCleanup=Export-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop
        $creNs=New-Object Xml.XmlNamespaceManager($creCleanup.NameTable)
        $creNs.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
        if ($creCleanup.SelectSingleNode('/t:Task/t:RegistrationInfo/t:Description',$creNs).InnerText -cne ('Private synthetic CRE4265 fixture '+$creFixtureId) -or
            $creCleanup.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId',$creNs).InnerText -cne $creSid -or
            $creCleanup.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments',$creNs).InnerText -cne $creFixtureDoc.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments',$creNs).InnerText) {
            $creNativeResult.cleanup='refused_identity_drift'
        } else {
            [void](Disable-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -ErrorAction Stop)
            $creCleanupService=New-Object -ComObject 'Schedule.Service'
            $creCleanupService.Connect()
            $creOwnedTask=$creCleanupService.GetFolder('\').GetTask($creFixtureName)
            $creCleanupWait=[Diagnostics.Stopwatch]::StartNew()
            while ($creOwnedTask.GetInstances(0).Count -ne 0 -and $creCleanupWait.Elapsed.TotalSeconds -lt 20) { Start-Sleep -Milliseconds 200 }
            if ($creOwnedTask.GetInstances(0).Count -ne 0) { $creNativeResult.cleanup='disabled_but_still_running' }
            else {
                Unregister-ScheduledTask -TaskName $creFixtureName -TaskPath '\' -Confirm:$false -ErrorAction Stop
                $creRemaining=@(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -ieq $creFixtureName })
                $creNativeResult.cleanup=if ($creRemaining.Count -eq 0) { 'removed_and_absence_verified' } else { 'unexpected_remaining_task' }
            }
        }
    }
    $creNativeResult | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ProofDirectory 'native.json') -Encoding UTF8
}
Write-Output ('Native scheduled-task '+$creNativeResult.status+'; cleanup='+$creNativeResult.cleanup+'; proof='+$ProofDirectory)
