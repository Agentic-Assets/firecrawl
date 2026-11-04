<# Offline native proof only. Use a fresh -NoProfile -File host. No real curl,
   WSL, provider, database, scheduler or external destination is invoked. #>
[CmdletBinding()]
param([string]$ProofDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows native tests only.' }
if (-not $ProofDirectory) { $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/2026-10-13-notification-' + [Guid]::NewGuid().ToString('N')) }
New-Item -ItemType Directory -Path $ProofDirectory -Force | Out-Null
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$privateRoot = Join-Path ([IO.Path]::GetTempPath()) ('cre-notification-proof-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $privateRoot | Out-Null
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$script:checks = 0
function Assert-True($condition, [string]$message) { if (-not $condition) { throw $message }; $script:checks++ }
function New-EndpointFile([string]$Name, [string]$Contents, [switch]$Unsafe) {
    $path = Join-Path $privateRoot $Name
    [IO.File]::WriteAllText($path, $Contents, (New-Object Text.UTF8Encoding($false)))
    $acl = New-Object Security.AccessControl.FileSecurity
    $sddl = "O:${sid}D:P(A;;FA;;;${sid})(A;;FA;;;SY)(A;;FA;;;BA)"
    if ($Unsafe) { $sddl += '(A;;FR;;;BU)' }
    $acl.SetSecurityDescriptorSddlForm($sddl)
    Set-Acl -LiteralPath $path -AclObject $acl
    return $path
}
$notification = Join-Path $PSScriptRoot 'CreWindowsNotification.cs'
$captureHelper = Join-Path $PSScriptRoot 'CreNativeCapture.cs'
Add-Type -TypeDefinition ([IO.File]::ReadAllText($notification))
Add-Type -TypeDefinition ([IO.File]::ReadAllText($captureHelper))
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$curl = Join-Path $privateRoot 'curl-200.exe'
& $compiler /nologo /target:exe "/out:$curl" (Join-Path $PSScriptRoot 'tests/NotificationFixture.cs')
if ($LASTEXITCODE -ne 0) { throw 'Synthetic curl compilation failed.' }
$worker = Join-Path $privateRoot 'worker.exe'
& $compiler /nologo /target:exe "/out:$worker" (Join-Path $PSScriptRoot 'tests/CaptureFixture.cs')
if ($LASTEXITCODE -ne 0) { throw 'Synthetic worker compilation failed.' }
$endpoint = 'https://example.invalid/SECRET_CANARY?x=%7Bone,two%7D&q=%5B1-2%5D'
$valid = New-EndpointFile 'valid.url' ($endpoint + "`r`n")
$payload = '{"text":"fixed synthetic failure"}'
$excluded = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
Assert-True ([CreWindowsNotification.Sender]::IsConfigured($valid, $excluded)) 'Private configuration check must accept a valid file without returning its URL.'
Assert-True (-not [IO.File]::Exists($curl + '.calls.jsonl')) 'Configuration validation must not start transport.'
Assert-True ([CreWindowsNotification.Sender]::Send($null, $excluded, 'missing.exe', $payload).Status -ceq 'not_configured') 'Unconfigured notification must not read or start transport.'
$sent = [CreWindowsNotification.Sender]::Send($valid, $excluded, $curl, $payload)
Assert-True ($sent.Status -ceq 'http_accepted' -and $sent.HttpStatus -eq 200 -and $sent.NativeSettlement -ceq 'exited') 'Private HTTPS endpoint plus curl0/HTTP200 means HTTP acceptance only.'
$calls = @([IO.File]::ReadAllLines($curl + '.calls.jsonl'))
$transport = $calls[0] | ConvertFrom-Json
Assert-True ($calls.Count -eq 1 -and -not $transport.secretArg -and $transport.hasUrl -and $transport.hasData) 'Endpoint and JSON data must reach stdin, never argv.'
Assert-True ($transport.disableFirst -and $transport.globOff -and $transport.timeout10 -and $transport.retry0 -and -not $transport.redirectOrInsecure) 'Fixed curl flags must refuse curlrc, glob expansion, redirects, retries and insecure TLS.'
Assert-True ([CreWindowsNotification.Sender]::EscapeConfig("a\`"b`n`t") -ceq 'a\\\"b\n\t') 'Curl config must escape backslashes before quotes and controls.'
foreach ($case in @(
    @('unsafe.url', $endpoint, $true), @('empty.url', '', $false), @('oversize.url', ('x' * 4097), $false),
    @('multiline.url', ($endpoint + "`n" + $endpoint), $false), @('http.url', 'http://example.invalid/SECRET_CANARY', $false),
    @('quote.url', 'https://example.invalid/SECRET_CANARY"', $false), @('glob.url', 'https://example.invalid/SECRET_CANARY/{one,two}', $false),
    @('userinfo.url', 'https://SECRET_CANARY@example.invalid/', $false), @('control.url', ($endpoint + [char]0), $false)
)) {
    $file = New-EndpointFile $case[0] $case[1] -Unsafe:([bool]$case[2])
    Assert-True (-not [CreWindowsNotification.Sender]::IsConfigured($file, $excluded)) 'Configuration validation must reject the same malformed/private-file cases as transport.'
    $rejected = [CreWindowsNotification.Sender]::Send($file, $excluded, $curl, $payload)
    Assert-True ($rejected.Status -ceq 'failed' -and $rejected.Reason -ceq 'private_endpoint_rejected') 'Unsafe or malformed endpoint must refuse before transport.'
}
foreach ($file in @((Join-Path $privateRoot 'missing.url'), $privateRoot, 'relative.url', ($valid + ':stream'))) {
    Assert-True (-not [CreWindowsNotification.Sender]::IsConfigured($file, $excluded)) 'Configuration validation must reject missing/nonregular/relative/alternate-stream inputs.'
    Assert-True ([CreWindowsNotification.Sender]::Send($file, $excluded, $curl, $payload).Reason -ceq 'private_endpoint_rejected') 'Missing, nonregular, relative or alternate-stream endpoint must be rejected.'
}
$gitDirectory = Join-Path $privateRoot 'repo'
New-Item -ItemType Directory -Path (Join-Path $gitDirectory '.git') -Force | Out-Null
$gitEndpoint = New-EndpointFile 'repo/inside.url' $endpoint
Assert-True ([CreWindowsNotification.Sender]::Send($gitEndpoint, $excluded, $curl, $payload).Reason -ceq 'private_endpoint_rejected') 'Endpoint must be outside any Git worktree, including another repository.'
$linked = New-EndpointFile 'multi-linked.url' $endpoint
New-Item -ItemType HardLink -Path (Join-Path $gitDirectory 'linked.url') -Target $linked | Out-Null
Assert-True ([CreWindowsNotification.Sender]::Send($linked, $excluded, $curl, $payload).Reason -ceq 'private_endpoint_rejected') 'Multi-link file must refuse an outside-Git alias of Git-contained data.'
Assert-True (@([IO.File]::ReadAllLines($curl + '.calls.jsonl')).Count -eq 1) 'Rejected endpoints must not dispatch curl.'
foreach ($sddl in @("O:SYD:P(A;;FA;;;${sid})", "O:${sid}D:(A;;FR;;;WD)")) {
    $descriptor = New-Object Security.AccessControl.RawSecurityDescriptor($sddl)
    $bytes = New-Object byte[] $descriptor.BinaryLength
    $descriptor.GetBinaryForm($bytes, 0)
    Assert-True (-not [CreWindowsNotification.Sender]::IsPrivateDescriptor($bytes, $sid)) 'Wrong owner or broad allow ACL must not be private.'
}
foreach ($mode in @('204', '302', '500', '28', 'noise', 'extra', 'flood')) {
    $fake = Join-Path $privateRoot ("curl-$mode.exe")
    [IO.File]::Copy($curl, $fake)
    $result = [CreWindowsNotification.Sender]::Send($valid, $excluded, $fake, $payload)
    $status = if ($mode -in @('204', 'noise')) { 'http_accepted' } else { 'failed' }
    Assert-True ($result.Status -ceq $status -and $result.NativeSettlement -ceq 'exited') 'Only exact three-digit2xx and native0 may be HTTP accepted.'
}

# Full launcher body; readiness, telemetry and executable resolution are offline seams.
$launcherText = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($launcherText, [ref]$tokens, [ref]$errors)
Assert-True ($errors.Count -eq 0) 'Launcher must parse.'
$admission = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-CreAdmission' }, $true))[0]
$readiness = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-CreLocalApi' }, $true))[0]
$hostExecutable = (Get-Process -Id $PID).Path
# The user-facing pipeline check uses the same helper, with a nonzero exit for
# missing/empty/unsafe configuration. All credentials remain synthetic here.
$configScript = Join-Path $PSScriptRoot 'Test-CreNotificationConfig.ps1'
foreach ($configCase in @('valid','missing','empty','unsafe','multiline','oversize')) {
    $configFile = Join-Path $privateRoot ($configCase + '.url')
    $configOut = Join-Path $ProofDirectory ($configCase + '.config.stdout')
    $configErr = Join-Path $ProofDirectory ($configCase + '.config.stderr')
    $checked = [CreWindowsCapture.Capture]::Run($hostExecutable,
        @('-NoProfile','-File',$configScript,'-AlertWebhookFile',$configFile), $configOut, $configErr, $null)
    $configured = $configCase -eq 'valid'
    Assert-True (-not $checked.Failed -and $checked.NativeExitCode -eq $(if ($configured) {0} else {1})) 'Configuration command must return a truthful exit code without transport.'
    $output = [IO.File]::ReadAllText($configOut)
    $configRecord = $output | ConvertFrom-Json
    Assert-True ($configRecord.configured -eq $configured -and -not $configRecord.networkCalled -and -not ($output + [IO.File]::ReadAllText($configErr)).Contains('SECRET_CANARY')) 'Configuration output must contain only redacted validity and no network or credential data.'
}
Assert-True (@([IO.File]::ReadAllLines($curl + '.calls.jsonl')).Count -eq 1) 'Configuration commands must not invoke the synthetic transport.'
foreach ($case in @('success', 'native-failed', 'notification-failed', 'bad-endpoint', 'capture-failed', 'denied', 'telemetry-error', 'missing-wsl', 'start-failed', 'initial-audit-error', 'terminal-audit-error', 'check-only', 'check-denied', 'check-error')) {
    $caseRoot = Join-Path $ProofDirectory $case; $caseWindows = Join-Path $caseRoot 'windows'
    New-Item -ItemType Directory -Path $caseWindows -Force | Out-Null
    [IO.File]::Copy($notification, (Join-Path $caseWindows 'CreWindowsNotification.cs'))
    [IO.File]::Copy($captureHelper, (Join-Path $caseWindows 'CreNativeCapture.cs'))
    $fakeCurl = Join-Path $privateRoot ("$case-curl.exe"); [IO.File]::Copy($curl, $fakeCurl)
    if ($case -eq 'notification-failed') {
        $curlDirectory = Join-Path $privateRoot $case
        New-Item -ItemType Directory -Path $curlDirectory | Out-Null
        $fakeCurl = Join-Path $curlDirectory 'curl-28.exe'; [IO.File]::Copy($curl, $fakeCurl)
    }
    $selectedWorker = $worker
    if ($case -in @('native-failed', 'notification-failed', 'bad-endpoint')) { $selectedWorker = Join-Path $caseRoot 'nonzero.exe'; [IO.File]::Copy($worker, $selectedWorker) }
    if ($case -eq 'capture-failed') { $selectedWorker = Join-Path $caseRoot 'held-pipes.exe'; [IO.File]::Copy($worker, $selectedWorker) }
    if ($case -eq 'start-failed') { $selectedWorker = Join-Path $caseRoot 'missing.exe' }
    $stub = "function Get-CreAdmission { return [ordered]@{ admitted = `$true; scope = 'synthetic' } }"
    if ($case -in @('denied', 'check-denied')) { $stub = $stub.Replace('admitted = $true', 'admitted = $false') }
    if ($case -in @('telemetry-error', 'check-error')) { $stub = "function Get-CreAdmission { throw 'synthetic telemetry failure' }" }
    $resolverMarker = ($fakeCurl + '.resolved').Replace("'", "''")
    $stub += "`nfunction Get-Command { param(`$Name, `$CommandType, `$ErrorAction) if (`$Name -eq 'curl.exe') { [IO.File]::WriteAllText('$resolverMarker','resolved'); [pscustomobject]@{Source='" + $fakeCurl.Replace("'", "''") + "'} } else { [pscustomobject]@{Source='" + $selectedWorker.Replace("'", "''") + "'} } }"
    if ($case -eq 'missing-wsl') { $stub += "`nfunction Get-Command { param(`$Name, `$CommandType, `$ErrorAction) if (`$Name -eq 'curl.exe') { [pscustomobject]@{Source='" + $fakeCurl.Replace("'", "''") + "'} } else { throw 'synthetic missing WSL' } }" }
    $source = $launcherText.Substring(0, $admission.Extent.StartOffset) + $stub + $launcherText.Substring($admission.Extent.EndOffset)
    $source = $source.Replace($readiness.Extent.Text, 'function Wait-CreLocalApi { return [ordered]@{ ready = $true; scope = "synthetic" } }')
    if ($case -in @('initial-audit-error', 'terminal-audit-error')) {
        $ordinal = if ($case -eq 'initial-audit-error') { 1 } else { 4 }
        $source = $source.Replace('function Write-CreAudit {', 'function Write-CreAuditReal {')
        $wrapper = "`n`$script:auditCalls=0`nfunction Write-CreAudit { param([string]`$Path,[System.Collections.IDictionary]`$Record) `$script:auditCalls++; if (`$script:auditCalls -eq $ordinal) { throw 'synthetic audit fault' }; Write-CreAuditReal `$Path `$Record }`n"
        $source = $source.Replace('if ($CheckOnly) {', $wrapper + 'if ($CheckOnly) {')
    }
    $scriptFile = Join-Path $caseWindows 'Invoke-CreGuardedTier.ps1'
    [IO.File]::WriteAllText($scriptFile, $source, (New-Object Text.UTF8Encoding($true)))
    $selectedEndpoint = if ($case -eq 'success' -or $case.StartsWith('check-') -or $case -eq 'bad-endpoint') { Join-Path $privateRoot 'must-not-read-missing.url' } else { $valid }
    $arguments = @('-NoProfile', '-File', $scriptFile, '-AlertWebhookFile', $selectedEndpoint)
    if ($case.StartsWith('check-')) { $arguments += '-CheckOnly' }
    else { $arguments += @('-Source', 'hanley', '-ExpectedSha', ('a' * 40)) }
    $out = Join-Path $caseRoot 'stdout'; $err = Join-Path $caseRoot 'stderr'
    if ($case -eq 'capture-failed') {
        # Only the outer test observer tolerates inherited ancestor handles;
        # production inner capture still uses its unchanged2s drain deadline.
        $outerOut = [IO.File]::Open($out, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $outerErr = [IO.File]::Open($err, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $execution = [CreWindowsCapture.Capture]::RunWithStreams($hostExecutable, $arguments, $outerOut, $outerErr, $null, 5000)
    } else { $execution = [CreWindowsCapture.Capture]::Run($hostExecutable, $arguments, $out, $err, $null) }
    Assert-True (-not $execution.Failed) 'Synthetic launcher host must drain.'
    $console = [IO.File]::ReadAllText($out) + [IO.File]::ReadAllText($err)
    Assert-True (-not $console.Contains('SECRET_CANARY')) 'Secret endpoint must never enter console output.'
    $callFile = $fakeCurl + '.calls.jsonl'
    $count = if ([IO.File]::Exists($callFile)) { @([IO.File]::ReadAllLines($callFile)).Count } else { 0 }
    if ($case -eq 'success' -or $case.StartsWith('check-')) {
        Assert-True ($count -eq 0) 'Successful invocation and CheckOnly must not send.'
        Assert-True (-not [IO.File]::Exists($fakeCurl + '.resolved')) 'Success and CheckOnly must not initialize notification/resolve curl before any secret read.'
        if ($case.StartsWith('check-')) { Assert-True (-not [IO.Directory]::Exists((Join-Path $caseRoot 'out'))) 'CheckOnly must write no audit/logs.' }
    } else {
        $expectedAttempts = if ($case -eq 'bad-endpoint') { 0 } else { 1 }
        Assert-True ($count -eq $expectedAttempts) 'Each eligible failure sends once; invalid private input sends none.'
        $auditFile = @(Get-ChildItem (Join-Path $caseRoot 'out/windows-launcher') -Filter '*.json')[0]
        $audit = [IO.File]::ReadAllText($auditFile.FullName)
        $record = $audit | ConvertFrom-Json
        $expectedStatus = if ($case -in @('notification-failed', 'bad-endpoint')) { 'failed' } else { 'http_accepted' }
        Assert-True (-not $audit.Contains('SECRET_CANARY') -and $record.notification.Status -ceq $expectedStatus -and $record.linuxSettlement -ceq 'unverified') 'Audit must contain only transport status and retain Linux uncertainty.'
        if ($case -eq 'capture-failed') { Assert-True ($record.nativeExitCode -eq 0 -and $record.wrapperExitCode -eq 1 -and $record.capture.Failed) 'Capture failure after native zero must notify without implying Linux settlement.' }
    }
    $expectedExit = if ($case -eq 'success' -or $case -eq 'check-only') { 0 } elseif ($case -in @('native-failed', 'notification-failed', 'bad-endpoint')) { 23 } elseif ($case -eq 'check-denied') { 75 } else { 1 }
    Assert-True ($execution.NativeExitCode -eq $expectedExit) 'Notification must preserve original launcher/native exit.'
}

# Last in this host: timeout uncertainty permanently refuses subsequent sends.
$hanging = Join-Path $privateRoot 'curl-hang.exe'; [IO.File]::Copy($curl, $hanging)
$clock = [Diagnostics.Stopwatch]::StartNew()
$timed = [CreWindowsNotification.Sender]::Send($valid, $excluded, $hanging, $payload)
$clock.Stop()
Assert-True ($timed.Status -ceq 'unverified' -and $timed.NativeSettlement -ceq 'unknown' -and $clock.Elapsed.TotalSeconds -lt 14) 'Native timeout must be bounded, retain unknown curl settlement and never claim acceptance.'
Assert-True ([CreWindowsNotification.Sender]::Send($valid, $excluded, $curl, $payload).Reason -ceq 'fresh_host_required') 'Unsettled notifier host must refuse reuse before another read/send.'
Write-Output "Windows notification PASS: $script:checks assertions; proof=$ProofDirectory; synthetic-private=$privateRoot"
