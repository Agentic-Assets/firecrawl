<# Shipped launcher with synthetic readiness/admission/native executables only.
   No real curl, WSL, scheduler, provider, database or Slack call is made. #>
[CmdletBinding()]
param([string]$ProofDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows native tests only.' }
if (-not $ProofDirectory) { $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/lifecycle-' + [Guid]::NewGuid().ToString('N')) }
[void](New-Item -ItemType Directory -Path $ProofDirectory -Force)
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$privateRoot = Join-Path ([IO.Path]::GetTempPath()) ('cre-lifecycle-' + [Guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $privateRoot)
$script:checks = 0
function Assert-Cre($Condition, [string]$Message) { if (-not $Condition) { throw $Message }; $script:checks++ }
$notification = Join-Path $PSScriptRoot 'CreWindowsNotification.cs'
$capture = Join-Path $PSScriptRoot 'CreNativeCapture.cs'
Add-Type -TypeDefinition ([IO.File]::ReadAllText($capture))
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$curl = Join-Path $privateRoot 'curl.exe'
& $compiler /nologo /target:exe "/out:$curl" (Join-Path $PSScriptRoot 'tests/NotificationFixture.cs')
if ($LASTEXITCODE -ne 0) { throw 'Synthetic curl compilation failed.' }
$workerSource = Join-Path $privateRoot 'worker.cs'
[IO.File]::WriteAllText($workerSource, @'
using System;
using System.Diagnostics;
class Worker {
    static int Main() {
        string mode = Process.GetCurrentProcess().MainModule.FileName;
        Console.WriteLine("/home/cayman/code/firecrawl/scripts/firecrawl-ops/cre_collector/out/guarded-tiers/2026-10-15T123456Z");
        if (mode.Contains("conflict")) Console.WriteLine("/home/cayman/code/firecrawl/scripts/firecrawl-ops/cre_collector/out/guarded-tiers/2026-10-15T123457Z");
        return mode.Contains("nonzero") ? 23 : 0;
    }
}
'@)
$worker = Join-Path $privateRoot 'worker.exe'
& $compiler /nologo /target:exe "/out:$worker" $workerSource
if ($LASTEXITCODE -ne 0) { throw 'Synthetic worker compilation failed.' }
$secretFile = Join-Path $privateRoot 'private.url'
[IO.File]::WriteAllText($secretFile, 'https://example.invalid/SECRET_CANARY', (New-Object Text.UTF8Encoding($false)))
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = New-Object Security.AccessControl.FileSecurity
$acl.SetSecurityDescriptorSddlForm("O:${sid}D:P(A;;FA;;;${sid})(A;;FA;;;SY)(A;;FA;;;BA)")
Set-Acl -LiteralPath $secretFile -AclObject $acl
$launcher = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($launcher,[ref]$tokens,[ref]$errors)
Assert-Cre ($errors.Count -eq 0) 'Launcher must parse.'
$admission=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-CreAdmission'},$true))[0]
$readiness=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Wait-CreLocalApi'},$true))[0]
$hostExe=(Get-Process -Id $PID).Path
$cases=@('success','native-failed','notification-failed','bad-endpoint','denied','not-ready','invalid-scope','invalid-mode','unconfigured','initial-audit-error','terminal-audit-error','notification-audit-error','persistent-audit-error','run-conflict','check-only','check-denied','check-error','default-success')
foreach ($case in $cases) {
    $root=Join-Path $ProofDirectory $case; $windows=Join-Path $root 'windows'
    [void](New-Item -ItemType Directory -Path $windows -Force)
    [IO.File]::Copy($notification,(Join-Path $windows 'CreWindowsNotification.cs'))
    [IO.File]::Copy($capture,(Join-Path $windows 'CreNativeCapture.cs'))
    $fakeCurl=Join-Path $privateRoot ($case+'-curl.exe')
    if ($case -eq 'notification-failed') {
        $folder=Join-Path $privateRoot $case; [void](New-Item -ItemType Directory -Path $folder)
        $fakeCurl=Join-Path $folder 'curl-28.exe'
    }
    [IO.File]::Copy($curl,$fakeCurl)
    $fakeWorker=Join-Path $root $(if ($case -eq 'native-failed') {'nonzero.exe'} elseif ($case -eq 'run-conflict') {'conflict.exe'} else {'worker.exe'})
    [IO.File]::Copy($worker,$fakeWorker)
    $eventFile=Join-Path $root 'events.log'
    $env:CRE_TEST_NOTIFICATION_EVENTS=$eventFile
    $eventLiteral=$eventFile.Replace("'","''")
    $admitted=if($case -eq 'denied' -or $case -eq 'check-denied'){'$false'}else{'$true'}
    $body=if($case -eq 'check-error'){ "throw 'synthetic admission error'" }else{ "return [ordered]@{admitted=$admitted}" }
    $stub="function Get-CreAdmission { [IO.File]::AppendAllText('$eventLiteral',('admit'+[Environment]::NewLine)); $body }"
    $resolverMarker=($fakeCurl+'.resolved').Replace("'","''")
    $stub+="`nfunction Get-Command { param(`$Name,`$CommandType,`$ErrorAction) if (`$Name -eq 'curl.exe') { [IO.File]::WriteAllText('$resolverMarker','resolved'); [pscustomobject]@{Source='"+$fakeCurl.Replace("'","''")+"'} } else { [pscustomobject]@{Source='"+$fakeWorker.Replace("'","''")+"'} } }"
    $source=$launcher.Substring(0,$admission.Extent.StartOffset)+$stub+$launcher.Substring($admission.Extent.EndOffset)
    $ready=if($case -eq 'not-ready'){'$false'}else{'$true'}
    $readinessStub="function Wait-CreLocalApi { [IO.File]::AppendAllText('$eventLiteral',('ready'+[Environment]::NewLine)); return [ordered]@{ready=$ready} }"
    $source=$source.Replace($readiness.Extent.Text,$readinessStub)
    $source=$source.Replace('function Write-CreAudit {','function Write-CreAuditReal {')
    $fault=if($case -eq 'initial-audit-error'){'$script:auditCalls -eq 1'}elseif($case -eq 'terminal-audit-error'){'$script:auditCalls -eq 4'}elseif($case -eq 'notification-audit-error'){'$script:auditCalls -eq 5'}elseif($case -eq 'persistent-audit-error'){'$script:auditCalls -ge 4'}else{'$false'}
    $auditStub="`n`$script:auditCalls=0`nfunction Write-CreAudit { param([string]`$Path,[System.Collections.IDictionary]`$Record) `$script:auditCalls++; [IO.File]::AppendAllText('$eventLiteral',('audit-'+`$script:auditCalls+[Environment]::NewLine)); if($fault){throw 'synthetic audit fault'}; Write-CreAuditReal `$Path `$Record }`n"
    $source=$source.Replace('if ($CheckOnly) {',$auditStub+'if ($CheckOnly) {')
    $scriptFile=Join-Path $windows 'Invoke-CreGuardedTier.ps1'
    [IO.File]::WriteAllText($scriptFile,$source,(New-Object Text.UTF8Encoding($true)))
    $endpoint=if($case -eq 'bad-endpoint' -or $case.StartsWith('check-') -or $case -eq 'default-success'){Join-Path $privateRoot 'missing.url'}else{$secretFile}
    $arguments=@('-NoProfile','-File',$scriptFile,'-AlertWebhookFile',$endpoint)
    if($case -ne 'default-success'){$arguments+=@('-NotificationMode',$(if($case -eq 'invalid-mode'){'invalid'}else{'StartFinish'}))}
    if($case -eq 'unconfigured'){$arguments=@('-NoProfile','-File',$scriptFile,'-NotificationMode','StartFinish')}
    if($case.StartsWith('check-')){$arguments+='-CheckOnly'}else{$arguments+=@('-Source','hanley','-ExpectedSha',('a'*40))}
    if($case -eq 'invalid-scope'){$arguments+=@('-Tier','series')}
    $stdout=Join-Path $root 'stdout'; $stderr=Join-Path $root 'stderr'
    $execution=[CreWindowsCapture.Capture]::Run($hostExe,$arguments,$stdout,$stderr,$null)
    Assert-Cre (-not $execution.Failed) "$case host must drain."
    $console=[IO.File]::ReadAllText($stdout)+[IO.File]::ReadAllText($stderr)
    Assert-Cre (-not $console.Contains('SECRET_CANARY')) "$case console must exclude credentials."
    $calls=@(if([IO.File]::Exists($fakeCurl+'.calls.jsonl')){[IO.File]::ReadAllLines($fakeCurl+'.calls.jsonl')|ForEach-Object{$_|ConvertFrom-Json}})
    $expected=if($case -in @('bad-endpoint','invalid-mode','unconfigured','check-only','check-denied','check-error','default-success')){0}elseif($case -in @('invalid-scope','initial-audit-error')){1}else{2}
    Assert-Cre ($calls.Count -eq $expected) "$case must attempt each eligible phase once."
    $exit=if($case -in @('success','notification-failed','bad-endpoint','notification-audit-error','run-conflict','check-only','default-success')){0}elseif($case -eq 'native-failed'){23}elseif($case -eq 'check-denied'){75}else{1}
    Assert-Cre ($execution.NativeExitCode -eq $exit) "$case notification must preserve the original operation exit."
    foreach($call in $calls){Assert-Cre (-not $call.secretArg -and $call.hasUrl -and $call.hasData) "$case secret/data must use stdin exclusively."}
    $events=@(if([IO.File]::Exists($eventFile)){[IO.File]::ReadAllLines($eventFile)})
    if($expected -eq 2){
        Assert-Cre ($calls[0].started -and $calls[0].noDispatchClaim -and -not $calls[1].started) "$case start must accurately precede Linux dispatch."
        Assert-Cre ($calls[0].attemptId -ceq $calls[1].attemptId -and $calls[0].attemptId -match '^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$') "$case phases must use the identical attempt ID."
        Assert-Cre (($events -join ',').StartsWith('audit-1,notify-start,audit-2,ready')) "$case start must follow durable initial audit and precede readiness/admission."
        $terminalAudit=if($case -in @('denied','not-ready')){'audit-3'}else{'audit-4'}
        Assert-Cre ([Array]::IndexOf($events,$terminalAudit) -ge 0 -and [Array]::IndexOf($events,'notify-terminal') -gt [Array]::IndexOf($events,$terminalAudit)) "$case terminal send must follow terminal audit attempt."
        Assert-Cre ($calls[1].finished -eq ($exit -eq 0) -and $calls[1].failed -eq ($exit -ne 0)) "$case terminal outcome must reflect wrapper exit."
        if($case -in @('terminal-audit-error','persistent-audit-error')){Assert-Cre $calls[1].auditFailure "$case must explicitly identify failed terminal audit."}
        if($case -in @('success','run-conflict')){Assert-Cre ($calls[1].informationalRun -eq ($case -eq 'success')) 'Only one unambiguous observed run reference may be included.'}
    }
    if($case -in @('initial-audit-error','invalid-scope')){Assert-Cre (-not $calls[0].started -and $calls[0].failed -and $events -notcontains 'ready' -and $events -notcontains 'admit') "$case must not send start or reach readiness/admission."}
    $audits=@(Get-ChildItem -LiteralPath (Join-Path $root 'out/windows-launcher') -Filter '*.json' -ErrorAction SilentlyContinue)
    if($case.StartsWith('check-') -or $case -eq 'invalid-mode'){
        Assert-Cre ($audits.Count -eq 0 -and -not [IO.File]::Exists($fakeCurl+'.resolved')) "$case must never initialize transport, read credentials or publish audits."
    }else{
        Assert-Cre ($audits.Count -eq 1) "$case must retain one attempt audit."
        $json=[IO.File]::ReadAllText($audits[0].FullName); $record=$json|ConvertFrom-Json
        Assert-Cre (-not $json.Contains('SECRET_CANARY') -and $record.linuxSettlement -ceq 'unverified') "$case audit must exclude secrets and preserve Linux uncertainty."
        if($expected -eq 2 -and $case -notin @('notification-audit-error','persistent-audit-error')){
            $status=if($case -eq 'notification-failed'){'failed'}else{'http_accepted'}
            Assert-Cre ($record.notificationStart.Status -ceq $status -and $record.notification.Status -ceq $status) "$case must independently persist both transport outcomes."
        }
    }
}
Remove-Item Env:CRE_TEST_NOTIFICATION_EVENTS
Write-Output "Windows lifecycle notification PASS: $script:checks assertions; proof=$ProofDirectory; synthetic-private=$privateRoot"
