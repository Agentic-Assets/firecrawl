<# Synthetic only. Run in a fresh powershell.exe / pwsh.exe -NoProfile -File host.
   Leaves proof in ignored .scratch; never invokes WSL or a CRE worker. #>
[CmdletBinding()]
param([string]$ProofDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows native tests only.' }
if (-not $ProofDirectory) {
    $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/2026-10-13-native-capture-' + [Guid]::NewGuid().ToString('N'))
}
New-Item -ItemType Directory -Path $ProofDirectory -Force | Out-Null
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$helper = Join-Path $PSScriptRoot 'CreNativeCapture.cs'
Add-Type -TypeDefinition ([IO.File]::ReadAllText($helper))
Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'tests/CaptureTestStreams.cs')))
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$executable = Join-Path $ProofDirectory 'synthetic child.exe'
& $compiler /nologo /target:exe "/out:$executable" (Join-Path $PSScriptRoot 'tests/CaptureFixture.cs')
if ($LASTEXITCODE -ne 0) { throw 'Synthetic native fixture compilation failed.' }
$script:checks = 0
function Assert-True($condition, [string]$message) {
    if (-not $condition) { throw $message }; $script:checks++
}
function Invoke-Capture([string[]]$ChildArguments) {
    $stem = Join-Path $ProofDirectory ([Guid]::NewGuid().ToString('N'))
    $capture = [CreWindowsCapture.Capture]::Run($executable, $ChildArguments, "$stem.out", "$stem.err", '/synthetic/out/guarded-tiers/')
    return [pscustomobject]@{ Result = $capture; Output = "$stem.out"; Error = "$stem.err" }
}
$test = Invoke-Capture @('flood')
Assert-True (-not $test.Result.Failed -and $test.Result.NativeExitCode -eq 0) 'Flood must return without deadlock.'
foreach ($stream in @($test.Result.Stdout, $test.Result.Stderr)) {
    Assert-True ($stream.ObservedBytes -eq 2621440 -and $stream.CapturedBytes -eq 1048576 -and $stream.Truncated -and $stream.Eof) 'Concurrent no-newline flood must be capped and fully drained.'
}
Assert-True ((Get-Item $test.Output).Length -eq 1048576 -and (Get-Item $test.Error).Length -eq 1048576) 'Both disk caps are exact.'
$values = @('', 'two words', 'a"b', 'a\"b', 'a\\\"b', '&|<>^%PATH%$(literal);`', 'C:\space path\', '\\server\path\\', 'caf' + [char]0xe9, [string][char]0x2603)
$test = Invoke-Capture (@('argv') + $values)
$actual = @([IO.File]::ReadAllLines($test.Output))
Assert-True ($actual.Count -eq $values.Count) 'Argument count must survive quoting.'
for ($i = 0; $i -lt $values.Count; $i++) {
    Assert-True ($actual[$i] -ceq [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($values[$i]))) 'Argument bytes must survive quoting.'
}
$test = Invoke-Capture @('utf8')
$expected = 'snowman ' + [char]0x2603 + ' astral ' + [char]::ConvertFromUtf32(0x1f680) + ' caf' + [char]0xe9 + "`n"
Assert-True ([IO.File]::ReadAllText($test.Output, [Text.Encoding]::UTF8) -ceq $expected) 'Raw UTF8 output must survive.'
$test = Invoke-Capture @('default')
Assert-True ($test.Result.NativeExitCode -eq 0 -and $test.Result.Stderr.ObservedBytes -gt 0 -and -not $test.Result.Failed) 'Stderr with exit zero is not a capture failure.'
$test = Invoke-Capture @('nonzero')
Assert-True ($test.Result.NativeExitCode -eq 23 -and -not $test.Result.Failed) 'Native nonzero must be retained separately.'
$preopenOut = Join-Path $ProofDirectory 'preopen.out'
$preopenError = Join-Path $ProofDirectory 'preopen.err'
$marker = Join-Path $ProofDirectory 'must-not-dispatch'
[IO.File]::WriteAllText($preopenError, 'existing sink')
$refused = $false
try { [CreWindowsCapture.Capture]::Run($executable, @('marker', $marker), $preopenOut, $preopenError, $null) | Out-Null }
catch { $refused = $true }
Assert-True ($refused -and -not [IO.File]::Exists($marker) -and [IO.File]::ReadAllText($preopenError) -ceq 'existing sink') 'Second-sink preopen failure must prevent dispatch and preserve existing files.'
$exclusive = [IO.File]::Open($preopenOut, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$exclusive.Dispose()
Assert-True ((Get-Item $preopenOut).Length -eq 0) 'Preopened first sink must close on second-sink failure.'
foreach ($fault in @('write', 'flush', 'close')) {
    $out = New-Object CreCaptureTests.FaultStream($fault); $err = New-Object IO.MemoryStream
    $failed = [CreWindowsCapture.Capture]::RunWithStreams($executable, @('flood'), $out, $err, $null, 2000)
    Assert-True ($failed.Failed -and $failed.NativeExitCode -eq 0 -and $failed.Stdout.ObservedBytes -eq 2621440 -and $failed.Stdout.Eof) 'Sink failure must keep draining and retain native exit.'
    Assert-True ($failed.Stdout.($fault.Substring(0,1).ToUpper() + $fault.Substring(1) + 'Failed')) 'Exact sink fault must be reported.'
}
$clock = [Diagnostics.Stopwatch]::StartNew()
$out = New-Object IO.MemoryStream; $err = New-Object IO.MemoryStream
$held = [CreWindowsCapture.Capture]::RunWithStreams($executable, @('held-pipes'), $out, $err, $null, 200)
$clock.Stop()
Assert-True ($held.NativeExitCode -eq 0 -and $held.Failed -and ($held.Stdout.DrainTimedOut -or $held.Stderr.DrainTimedOut)) 'Inherited handles must be explicit incomplete capture.'
Assert-True ($clock.Elapsed.TotalSeconds -lt 3) 'Inherited pipes must not block until descendant exit.'
Assert-True (-not $held.Stdout.ReaderDidNotStop -and -not $held.Stderr.ReaderDidNotStop) 'Exact reader cancellation must join in this fixture.'
$readerType = [CreWindowsCapture.Capture].GetNestedType('Reader', [Reflection.BindingFlags]::NonPublic)
$state = New-Object CreWindowsCapture.StreamResult
$inputStream = New-Object CreCaptureTests.ChunkStream
$outputStream = New-Object IO.MemoryStream
$reader = $readerType.GetConstructors()[0].Invoke([object[]]@($inputStream.PSObject.BaseObject, $outputStream.PSObject.BaseObject, $state.PSObject.BaseObject, '/synthetic/'))
$reader.Start(); $reader.Join(2000); $reader.ReleaseHandle()
Assert-True ($state.Eof -and -not $state.Failed -and $state.ObservedBytes -eq 8195) 'Split UTF8 scalar plus full buffer must drain without decoder overflow.'
$test = Invoke-Capture @('refs', '/synthetic/out/guarded-tiers/')
Assert-True ($test.Result.Stdout.ObservedRunDirectory -ceq '/synthetic/out/guarded-tiers/2026-10-13T183401Z-0123456789ab' -and $test.Result.Stdout.RunReferenceConflict) 'Run correlation must retain first exact hint and flag conflicting references.'

# Execute the actual launcher body in a private directory, replacing only its
# telemetry function and application resolver with deterministic offline seams.
# The shipped capture helper and all audit/dispatch/exit code remain unchanged.
$launcher = Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
$sourceText = [IO.File]::ReadAllText($launcher)
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($sourceText, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) 'Shipped launcher must parse.'
$admissionDefinition = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-CreAdmission'
}, $true))
Assert-True ($admissionDefinition.Count -eq 1) 'Exactly one telemetry seam is replaced.'
$readinessDefinition = @($ast.FindAll({
    param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-CreLocalApi'
}, $true))[0]
$auditDefinition = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Write-CreAudit'
}, $true))[0]
Invoke-Expression $auditDefinition.Extent.Text
$auditFile = Join-Path $ProofDirectory 'atomic-audit.json'
Write-CreAudit $auditFile ([ordered]@{ generation = 1; value = 'original' })
$original = [IO.File]::ReadAllText($auditFile)
$reader = [IO.File]::Open($auditFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
$refused = $false
try { Write-CreAudit $auditFile ([ordered]@{ generation = 2; value = 'replacement' }) }
catch { $refused = $true }
finally { $reader.Dispose() }
Assert-True ($refused -and [IO.File]::ReadAllText($auditFile) -ceq $original) 'Atomic replacement failure must preserve the complete previous audit.'
Assert-True (@(Get-ChildItem $ProofDirectory -Filter 'atomic-audit.json.*.tmp').Count -eq 0) 'Atomic failure must remove only its own temporary file.'
Write-CreAudit $auditFile ([ordered]@{ generation = 2; value = 'replacement' })
Assert-True (([IO.File]::ReadAllText($auditFile) | ConvertFrom-Json).generation -eq 2) 'Atomic replacement must publish parseable complete JSON.'
$lastValid = [IO.File]::ReadAllText($auditFile)
$refused = $false
try { Write-CreAudit $auditFile ([ordered]@{ value = ('x' * 65536) }) } catch { $refused = $true }
Assert-True ($refused -and [IO.File]::ReadAllText($auditFile) -ceq $lastValid) 'Oversized audit must refuse publication and preserve its last valid receipt.'
$hostExecutable = (Get-Process -Id $PID).Path
foreach ($scenario in @('admitted', 'native-nonzero', 'launch-failure', 'held-pipes', 'flood', 'post-start-error', 'cached-helper', 'denied', 'telemetry-error', 'missing-exe', 'missing-identity', 'terminal-audit-error', 'initial-audit-error', 'check-only', 'check-denied', 'check-error')) {
    $caseRoot = Join-Path $ProofDirectory $scenario
    $caseWindows = Join-Path $caseRoot 'windows'
    New-Item -ItemType Directory -Path $caseWindows -Force | Out-Null
    [IO.File]::Copy($helper, (Join-Path $caseWindows 'CreNativeCapture.cs'))
    if ($scenario -eq 'post-start-error') {
        # Explicit fault injection after actual synthetic native return; all
        # ordinary stream tests above execute the byte-identical shipped helper.
        $faultSource = [IO.File]::ReadAllText($helper).Replace('result.NativeExitCode = process.ExitCode;', 'if (process.Id > 0) throw new InvalidOperationException("synthetic post-start fault"); result.NativeExitCode = process.ExitCode;')
        [IO.File]::WriteAllText((Join-Path $caseWindows 'CreNativeCapture.cs'), $faultSource)
    }
    $stub = "function Get-CreAdmission { return [ordered]@{ admitted = `$true; admissionReason = 'memory_pressure'; cpuPercent = 10; scope = 'synthetic'; availablePhysicalGiB = 20 } }"
    if ($scenario -in @('denied', 'check-denied')) { $stub = $stub.Replace('admitted = $true', 'admitted = $false') }
    if ($scenario -in @('telemetry-error', 'check-error')) { $stub = "function Get-CreAdmission { throw 'CREDENTIAL_CANARY_DO_NOT_LOG' }" }
    $selectedExecutable = $executable
    if ($scenario -in @('native-nonzero', 'held-pipes', 'flood')) {
        $name = if ($scenario -eq 'native-nonzero') { 'nonzero' } else { $scenario }
        $selectedExecutable = Join-Path $caseRoot ($name + '.exe')
        [IO.File]::Copy($executable, $selectedExecutable)
    }
    if ($scenario -eq 'launch-failure') { $selectedExecutable = Join-Path $caseRoot 'missing.exe' }
    $stub += "`nfunction Get-Command { param(`$Name, `$CommandType, `$ErrorAction) [pscustomobject]@{ Source = '" + $selectedExecutable.Replace("'", "''") + "' } }"
    if ($scenario -eq 'missing-exe') { $stub += "`nfunction Get-Command { param(`$Name, `$CommandType, `$ErrorAction) throw 'CREDENTIAL_CANARY_DO_NOT_LOG' }" }
    $transformed = $sourceText.Substring(0, $admissionDefinition[0].Extent.StartOffset) + $stub + $sourceText.Substring($admissionDefinition[0].Extent.EndOffset)
    $transformed = $transformed.Replace($readinessDefinition.Extent.Text, 'function Wait-CreLocalApi { return [ordered]@{ ready = $true; scope = "synthetic" } }')
    if ($scenario -eq 'cached-helper') {
        $preload = "Add-Type -Path '" + (Join-Path $caseWindows 'CreNativeCapture.cs').Replace("'", "''") + "'`n"
        $transformed = $transformed.Replace('Set-StrictMode -Version Latest', $preload + 'Set-StrictMode -Version Latest')
    }
    if ($scenario -in @('initial-audit-error', 'terminal-audit-error')) {
        $ordinal = if ($scenario -eq 'initial-audit-error') { 1 } else { 4 }
        $transformed = $transformed.Replace('function Write-CreAudit {', 'function Write-CreAuditReal {')
        $wrapper = "`n`$script:auditCalls = 0`nfunction Write-CreAudit { param([string]`$Path, [System.Collections.IDictionary]`$Record) `$script:auditCalls++; if (`$script:auditCalls -eq $ordinal) { throw 'CREDENTIAL_CANARY_DO_NOT_LOG' }; Write-CreAuditReal `$Path `$Record }`n"
        $transformed = $transformed.Replace('if ($CheckOnly) {', $wrapper + 'if ($CheckOnly) {')
    }
    $caseScript = Join-Path $caseWindows 'Invoke-CreGuardedTier.ps1'
    [IO.File]::WriteAllText($caseScript, $transformed, (New-Object Text.UTF8Encoding($true)))
    $childArguments = @('-NoProfile', '-File', $caseScript)
    if ($scenario.StartsWith('check-')) { $childArguments += '-CheckOnly' }
    elseif ($scenario -ne 'missing-identity') { $childArguments += @('-Source', 'hanley', '-ExpectedSha', ('a' * 40)) }
    $caseOut = Join-Path $caseRoot 'host.stdout'
    $caseErr = Join-Path $caseRoot 'host.stderr'
    if ($scenario -eq 'held-pipes') {
        # Framework descendants can also inherit this outer harness's pipe
        # handles. Give only the test observer enough time for the4s synthetic
        # holder to exit. The inner production launcher retains its2s budget.
        $outerOutput = [IO.File]::Open($caseOut, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $outerError = [IO.File]::Open($caseErr, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $launched = [CreWindowsCapture.Capture]::RunWithStreams($hostExecutable, $childArguments, $outerOutput, $outerError, $null, 5000)
    } else {
        $launched = [CreWindowsCapture.Capture]::Run($hostExecutable, $childArguments, $caseOut, $caseErr, $null)
    }
    [IO.File]::WriteAllText((Join-Path $caseRoot 'host-capture.json'), ($launched | ConvertTo-Json -Depth 8))
    $audits = @(Get-ChildItem (Join-Path $caseRoot 'out/windows-launcher') -Filter '*.json' -ErrorAction SilentlyContinue)
    Assert-True (-not $launched.Failed) 'Synthetic launcher host output must drain.'
    Assert-True (([IO.File]::ReadAllText($caseOut) + [IO.File]::ReadAllText($caseErr)) -notmatch 'CREDENTIAL_CANARY_DO_NOT_LOG') 'Launcher diagnostics must not expose full exception details.'
    if ($scenario.StartsWith('check-')) {
        Assert-True (-not [IO.Directory]::Exists((Join-Path $caseRoot 'out'))) 'CheckOnly must write no audit or log directory.'
        $expectedCode = if ($scenario -eq 'check-only') { 0 } elseif ($scenario -eq 'check-denied') { 75 } else { 1 }
        Assert-True ($launched.NativeExitCode -eq $expectedCode) 'CheckOnly exit must distinguish admitted, denied and invalid telemetry.'
        continue
    }
    if ($scenario -eq 'initial-audit-error') {
        Assert-True ($launched.NativeExitCode -eq 1 -and $audits.Count -eq 0) 'Initial audit failure must refuse before native dispatch.'
        continue
    }
    Assert-True ($audits.Count -eq 1) 'Normal invocation must retain exactly one flat audit.'
    $receiptText = [IO.File]::ReadAllText($audits[0].FullName)
    $receipt = $receiptText | ConvertFrom-Json
    Assert-True ($receipt.linuxSettlement -ceq 'unverified' -and $receiptText -notmatch 'CREDENTIAL_CANARY_DO_NOT_LOG') 'Audit must never infer Linux settlement or include exception text.'
    if ($scenario -eq 'admitted') {
        Assert-True ($receipt.status -ceq 'returned' -and $receipt.dispatch -ceq 'returned' -and $receipt.nativeExitCode -eq 0 -and $receipt.wrapperExitCode -eq 0 -and $launched.NativeExitCode -eq 0) 'Native success must retain distinct successful wrapper/native results.'
        Assert-True ([IO.File]::Exists($receipt.stdoutPath) -and [IO.File]::Exists($receipt.stderrPath)) 'Same-basename stream sidecars must exist.'
        Assert-True ([IO.File]::ReadAllText($caseOut).Contains($receipt.stdoutPath)) 'Log paths must be visible to the foreground operator.'
        Assert-True ($receipt.captureHelperSha256 -ceq (Get-FileHash $helper -Algorithm SHA256).Hash.ToLowerInvariant()) 'Helper hash must bind the bytes compiled, not a separately reread file.'
        $admissionPending = [IO.File]::ReadAllText($caseScript).IndexOf("Write-CreAudit `$auditPath `$record")
        $sampleCall = [IO.File]::ReadAllText($caseScript).LastIndexOf('Wait-CreStartupAdmission $record $auditPath')
        Assert-True ($admissionPending -ge 0 -and $admissionPending -lt $sampleCall) 'Initial audit publication must precede admission telemetry.'
    } elseif ($scenario -eq 'native-nonzero') {
        Assert-True ($launched.NativeExitCode -eq 23 -and $receipt.nativeExitCode -eq 23 -and $receipt.wrapperExitCode -eq 23 -and $receipt.status -ceq 'returned') 'Native failure must preserve its actual exit separately from successful capture.'
    } elseif ($scenario -eq 'launch-failure') {
        Assert-True ($launched.NativeExitCode -eq 1 -and $null -eq $receipt.nativeExitCode -and $receipt.dispatch -ceq 'attempted' -and $receipt.capture.StartFailed) 'Native start attempt failure must remain attempted and Linux-unverified.'
    } elseif ($scenario -eq 'held-pipes') {
        Assert-True ($launched.NativeExitCode -eq 1 -and $receipt.nativeExitCode -eq 0 -and $receipt.wrapperExitCode -eq 1 -and $receipt.capture.Failed) 'Capture drain failure must make wrapper nonzero after native zero.'
        Assert-True (($receipt.capture.Stdout.DrainTimedOut -or $receipt.capture.Stderr.DrainTimedOut) -and -not $receipt.capture.Stdout.ReaderDidNotStop -and -not $receipt.capture.Stderr.ReaderDidNotStop) 'Inner production drain must time out and join its owned readers.'
    } elseif ($scenario -eq 'flood') {
        Assert-True ($launched.NativeExitCode -eq 0 -and $receipt.capture.Stdout.Truncated -and $receipt.capture.Stderr.Truncated -and -not $receipt.capture.Failed) 'Intentional truncation alone may retain native zero.'
    } elseif ($scenario -eq 'post-start-error') {
        Assert-True ($launched.NativeExitCode -eq 1 -and $receipt.dispatch -ceq 'pending' -and $null -eq $receipt.nativeExitCode -and $receipt.status -ceq 'windows_launcher_failed_linux_settlement_unverified') 'Post-start helper exception must retain possibly-dispatched uncertainty.'
    } elseif ($scenario -eq 'terminal-audit-error') {
        Assert-True ($launched.NativeExitCode -eq 1 -and $receipt.dispatch -ceq 'pending' -and $null -eq $receipt.wrapperExitCode) 'Terminal audit failure after native zero must be nonzero and preserve incomplete prior audit.'
    } else {
        $status = if ($scenario -eq 'denied') { 'admission_refused' } else { 'windows_launcher_failed_linux_settlement_unverified' }
        Assert-True ($launched.NativeExitCode -eq 1 -and $receipt.status -ceq $status -and $receipt.dispatch -ceq 'not_attempted' -and $null -eq $receipt.nativeExitCode) 'Admission or executable refusal must retain audit without native dispatch.'
        Assert-True (@(Get-ChildItem (Join-Path $caseRoot 'out/windows-launcher') -Filter '*.log').Count -eq 0) 'Pre-dispatch refusal must not create stream files.'
    }
}
# A managed blocking sink is deliberately not cancelled by CancelSynchronousIo.
# Do not reuse a host with unjoined resources, even if the operator invoked the
# launcher interactively and its `exit` returned to an outer caller.
$blocked = New-Object CreCaptureTests.BlockingStream
$clock = [Diagnostics.Stopwatch]::StartNew()
try {
    $uncertain = [CreWindowsCapture.Capture]::RunWithStreams($executable, @('default'), $blocked, (New-Object IO.MemoryStream), $null, 100)
    $clock.Stop()
    Assert-True ($uncertain.Failed -and $uncertain.Stdout.ReaderDidNotStop -and $clock.Elapsed.TotalSeconds -lt 3) 'Unjoined sink must return bounded failure without cross-thread disposal.'
    $refused = $false
    $poisonOut = Join-Path $ProofDirectory 'poison.out'; $poisonErr = Join-Path $ProofDirectory 'poison.err'
    try { [CreWindowsCapture.Capture]::Run($executable, @('default'), $poisonOut, $poisonErr, $null) | Out-Null }
    catch { $refused = $true }
    Assert-True ($refused -and -not [IO.File]::Exists($poisonOut) -and -not [IO.File]::Exists($poisonErr)) 'Poisoned host must refuse before opening files or dispatching another native child.'
} finally { $blocked.Release.Set() | Out-Null }
Write-Output "Native capture PASS: $script:checks assertions; $ProofDirectory"
