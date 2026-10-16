<# Offline native proof: shipped launcher body, synthetic admission and WSL
   executable only. No actual WSL, provider, database or scheduler is called. #>
[CmdletBinding()]
param([string]$ProofDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows native tests only.' }
if (-not $ProofDirectory) { $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/series-launcher-' + [Guid]::NewGuid().ToString('N')) }
New-Item -ItemType Directory -Path $ProofDirectory -Force | Out-Null
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$script:checks = 0
function Assert-True($condition, [string]$message) { if (-not $condition) { throw $message }; $script:checks++ }
Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'CreNativeCapture.cs')))
$fixtureSource = @'
using System;
using System.IO;
using System.Text;
class Fixture {
    static int Main(string[] args) {
        using (var stream = new FileStream(Environment.GetEnvironmentVariable("CRE_TEST_ARGV"), FileMode.CreateNew))
        using (var writer = new BinaryWriter(stream, new UTF8Encoding(false))) {
            writer.Write(args.Length); foreach (var arg in args) writer.Write(arg);
        }
        Console.WriteLine("{\"series_dir\":\"/tmp/synthetic series\",\"invocation_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}");
        return Int32.Parse(Environment.GetEnvironmentVariable("CRE_TEST_EXIT") ?? "0");
    }
}
'@
$fixtureFile = Join-Path $ProofDirectory 'fixture.cs'
$fixtureExe = Join-Path $ProofDirectory 'fixture.exe'
[IO.File]::WriteAllText($fixtureFile, $fixtureSource)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:exe "/out:$fixtureExe" $fixtureFile
if ($LASTEXITCODE -ne 0) { throw 'Synthetic WSL compilation failed.' }
$hostExecutable = (Get-Process -Id $PID).Path
$launcherText = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($launcherText, [ref]$tokens, [ref]$errors)
Assert-True ($errors.Count -eq 0) 'Launcher must parse.'
$admission = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-CreAdmission' }, $true))[0]
$readiness = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-CreLocalApi' }, $true))[0]
$sha = 'a' * 40
$series = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '3600', '-AcknowledgeDatabaseWrites')
$quotedRepo = '/home/synthetic space/quote''s "directory"/firecrawl'
$quotedEnv = '/tmp/synthetic space/quote''s "env".env'
$cases = @(
    @{ Name = 'essex-current'; Args = @('-Tier', 'series', '-Source', 'essex-brokerage', '-AdmitCurrentInventorySource', 'essex-brokerage', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 0; Dispatch = $true },
    @{ Name = 'essex-wrong-source'; Args = @('-Tier', 'series', '-Source', 'hanley', '-AdmitCurrentInventorySource', 'essex-brokerage', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'essex-wrong-tier'; Args = @('-Tier', 'monitor', '-Source', 'essex-brokerage', '-AdmitCurrentInventorySource', 'essex-brokerage', '-ExpectedSha', $sha); Exit = 1; Dispatch = $false },
    @{ Name = 'pyramid-current'; Args = @('-Tier', 'series', '-Source', 'pyramid-brokerage', '-AdmitCurrentInventorySource', 'pyramid-brokerage', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 0; Dispatch = $true },
    @{ Name = 'pyramid-wrong-source'; Args = @('-Tier', 'series', '-Source', 'hanley', '-AdmitCurrentInventorySource', 'pyramid-brokerage', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'pyramid-wrong-tier'; Args = @('-Tier', 'monitor', '-Source', 'pyramid-brokerage', '-AdmitCurrentInventorySource', 'pyramid-brokerage', '-ExpectedSha', $sha); Exit = 1; Dispatch = $false },
    @{ Name = 'series'; Args = $series; Exit = 0; Dispatch = $true },
    @{ Name = 'interra-current'; Args = @('-Tier', 'series', '-Source', 'interra-realty', '-AdmitCurrentInventorySource', 'interra-realty', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 0; Dispatch = $true },
    @{ Name = 'buildout-current'; Args = @('-Tier', 'series', '-Source', 'faris-lee', '-AdmitCurrentInventorySource', 'faris-lee', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 0; Dispatch = $true },
    @{ Name = 'buildout-wrong-source'; Args = @('-Tier', 'series', '-Source', 'faris-lee', '-AdmitCurrentInventorySource', 'kiser-group', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'buildout-wrong-tier'; Args = @('-Tier', 'monitor', '-Source', 'faris-lee', '-AdmitCurrentInventorySource', 'faris-lee', '-ExpectedSha', $sha); Exit = 1; Dispatch = $false },
    @{ Name = 'interra-wrong-source'; Args = $series + @('-AdmitCurrentInventorySource', 'interra-realty'); Exit = 1; Dispatch = $false },
    @{ Name = 'interra-wrong-tier'; Args = @('-Tier', 'monitor', '-Source', 'interra-realty', '-AdmitCurrentInventorySource', 'interra-realty', '-ExpectedSha', $sha); Exit = 1; Dispatch = $false },
    @{ Name = 'interra-invalid-option'; Args = $series + @('-AdmitCurrentInventorySource', 'hanley'); Exit = 1; Dispatch = $false },
    @{ Name = 'selected'; Args = @('-Tier', 'series', '-Source', 'hanley', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 0; Dispatch = $true },
    @{ Name = 'max-budget'; Args = @('-Tier', 'series', '-Source', 'hanley', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '86400', '-AcknowledgeDatabaseWrites'); Exit = 0; Dispatch = $true },
    @{ Name = 'native124'; Args = $series; Exit = 124; Dispatch = $true; Native = '124' },
    @{ Name = 'quoted-paths'; Args = $series + @('-LinuxRepo', $quotedRepo, '-LinuxEnvFile', $quotedEnv); Exit = 0; Dispatch = $true; LinuxRepo = $quotedRepo },
    @{ Name = 'missing-ack'; Args = $series[0..7]; Exit = 1; Dispatch = $false },
    @{ Name = 'missing-budget'; Args = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', $sha, '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'missing-sha'; Args = @('-Tier', 'series', '-Source', 'all', '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'missing-source'; Args = @('-Tier', 'series', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'series-apply'; Args = $series + '-Apply'; Exit = 1; Dispatch = $false },
    @{ Name = 'series-batch'; Args = $series + @('-Batch', '50'); Exit = 1; Dispatch = $false },
    @{ Name = 'series-incremental-deadline'; Args = $series + @('-DeadlineSeconds', '1800'); Exit = 1; Dispatch = $false },
    @{ Name = 'monitor-series-budget'; Args = @('-Source', 'hanley', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30'); Exit = 1; Dispatch = $false },
    @{ Name = 'enrich-series-ack'; Args = @('-Tier', 'enrich', '-Source', 'hanley', '-ExpectedSha', $sha, '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'monitor'; Args = @('-Source', 'hanley', '-ExpectedSha', $sha); Exit = 0; Dispatch = $true },
    @{ Name = 'enrich'; Args = @('-Tier', 'enrich', '-Source', 'hanley', '-ExpectedSha', $sha, '-Apply', '-Batch', '3', '-DeadlineSeconds', '90'); Exit = 0; Dispatch = $true },
    @{ Name = 'check-only'; Args = @('-Tier', 'series', '-CheckOnly', '-AlertWebhookFile', 'MISSING-SECRET-CANARY'); Exit = 0; Dispatch = $false; Check = $true },
    @{ Name = 'cpu-recovery'; Args = $series; Exit = 0; Dispatch = $true; CpuRecovery = $true },
    @{ Name = 'cpu-recovery-native124'; Args = $series; Exit = 124; Dispatch = $true; CpuRecovery = $true; Native = '124' },
    @{ Name = 'cpu-telemetry-error'; Args = $series; Exit = 1; Dispatch = $false; Denied = $true; TelemetryError = $true },
    @{ Name = 'cpu-wait-audit-failure'; Args = $series; Exit = 1; Dispatch = $false; Denied = $true; CpuRecovery = $true; AuditFailure = $true },
    @{ Name = 'denied'; Args = $series; Exit = 1; Dispatch = $false; Denied = $true },
    @{ Name = 'readiness-unavailable'; Args = $series; Exit = 1; Dispatch = $false; ReadinessDenied = $true },
    @{ Name = 'bad-budget'; Args = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '29', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'too-large-budget'; Args = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '86401', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'source-list'; Args = @('-Tier', 'series', '-Source', 'jll,hanley', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'bad-sha'; Args = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', 'main', '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false }
)
foreach ($concurrency in @(1,2,6)) {
    $cases += @{Name=('concurrency-' + $concurrency); Args=$series + @('-Concurrency', "$concurrency"); Exit=0; Dispatch=$true; Concurrency=$concurrency}
}
foreach ($invalid in @(0,7)) {
    $cases += @{Name=('concurrency-invalid-' + $invalid); Args=$series + @('-Concurrency', "$invalid"); Exit=1; Dispatch=$false}
}
foreach ($tier in @('monitor','enrich')) {
    $cases += @{Name=('concurrency-' + $tier); Args=@('-Tier',$tier,'-Source','hanley','-ExpectedSha',$sha,'-Concurrency','1'); Exit=1; Dispatch=$false}
}
$cases += @{Name='concurrency-denied'; Args=$series + @('-Concurrency','2'); Exit=1; Dispatch=$false; Denied=$true}
$cases += @{Name='concurrency-readiness-unavailable'; Args=$series + @('-Concurrency','6'); Exit=1; Dispatch=$false; ReadinessDenied=$true}
$savills = @('-Tier','series','-Source','savills','-AdmitBaselineHoldAdditively','-ExpectedSha',$sha,'-WorkDeadlineSeconds','30','-AcknowledgeDatabaseWrites')
$cases += @{Name='savills-hold'; Args=$savills; Exit=0; Dispatch=$true}
foreach ($bad in @(@('-Tier','monitor','-Source','savills'), @('-Tier','series','-Source','hanley'), @('-Tier','series','-Source','all'), @('-Tier','series','-Source','savills','-Pipeline'), @('-Tier','series','-Source','savills','-AdmitCurrentInventorySource','interra-realty'))) {
    $cases += @{Name=('savills-invalid-' + $cases.Count); Args=($bad + @('-AdmitBaselineHoldAdditively','-ExpectedSha',$sha,'-WorkDeadlineSeconds','30','-AcknowledgeDatabaseWrites')); Exit=1; Dispatch=$false}
}
$pipelineBase = @('-Tier','series','-Sources','cbre,jll','-Pipeline','-ExpectedSha',$sha,
    '-WorkDeadlineSeconds','30','-AcknowledgeDatabaseWrites')
$cases += @{Name='pipeline-concurrency'; Args=$pipelineBase + @('-SourceWorkers','2','-Concurrency','6'); Exit=0; Dispatch=$true; Workers=2; Concurrency=6}
$cases += @{Name='pipeline-default'; Args=$pipelineBase; Exit=0; Dispatch=$true; Workers=2}
foreach ($workers in @(1,2,3,4)) {
    $cases += @{Name=('pipeline-' + $workers); Args=($pipelineBase + @('-SourceWorkers',"$workers")); Exit=0; Dispatch=$true; Workers=$workers}
}
foreach ($workers in @(0,5)) {
    $cases += @{Name=('pipeline-invalid-' + $workers); Args=($pipelineBase + @('-SourceWorkers',"$workers")); Exit=1; Dispatch=$false}
}
$cases += @{Name='pipeline-duplicate'; Args=($series + @('-Pipeline','-Sources','jll,jll')); Exit=1; Dispatch=$false}
$cases += @{Name='pipeline-unknown'; Args=@('-Tier','series','-Pipeline','-Sources','jll,unknown','-ExpectedSha',$sha,'-WorkDeadlineSeconds','30','-AcknowledgeDatabaseWrites'); Exit=1; Dispatch=$false}
$cases += @{Name='pipeline-source-conflict'; Args=($series + @('-Pipeline','-Sources','cbre,jll')); Exit=1; Dispatch=$false}
$cases += @{Name='serial-workers'; Args=($series + @('-SourceWorkers','2')); Exit=1; Dispatch=$false}
foreach ($case in $cases) {
    $caseRoot = Join-Path $ProofDirectory $case.Name
    $caseWindows = Join-Path $caseRoot 'windows'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../data') -Destination (Join-Path $caseRoot 'data') -Recurse -Force
    New-Item -ItemType Directory -Path $caseWindows -Force | Out-Null
    [IO.File]::Copy((Join-Path $PSScriptRoot 'CreNativeCapture.cs'), (Join-Path $caseWindows 'CreNativeCapture.cs'))
    $argvPath = Join-Path $caseRoot 'argv.bin'
    $resolverPath = Join-Path $caseRoot 'resolved'
    $readinessPath = Join-Path $caseRoot 'readiness'
    $admissionPath = Join-Path $caseRoot 'admission'
    $env:CRE_TEST_ARGV = $argvPath
    $env:CRE_TEST_EXIT = if ($case.ContainsKey('Native')) { $case.Native } else { '0' }
    $admitted = if ($case.ContainsKey('Denied')) { '$false' } else { '$true' }
    $counter = "`$n=0; if ([IO.File]::Exists('" + $admissionPath.Replace("'", "''") + "')) { `$n=[int][IO.File]::ReadAllText('" + $admissionPath.Replace("'", "''") + "') }; `$n++; [IO.File]::WriteAllText('" + $admissionPath.Replace("'", "''") + "',[string]`$n);"
    $observation = "return [ordered]@{ admitted = $admitted; admissionReason='memory_pressure'; scope = 'synthetic' }"
    if ($case.ContainsKey('CpuRecovery')) {
        $observation = "return [ordered]@{ admitted=(`$n -ge 2); admissionReason=`$(if (`$n -ge 2) {'admitted'} else {'cpu_pressure'}); scope='synthetic' }"
    }
    if ($case.ContainsKey('TelemetryError')) { $observation = "throw 'Synthetic telemetry failure'" }
    $stub = "function Get-CreAdmission { if (-not `$CheckOnly -and -not [IO.File]::Exists('" + $readinessPath.Replace("'", "''") + "')) { throw 'Readiness must precede fresh admission' }; " + $counter + $observation + " }"
    $stub += "`nfunction Get-Command { param(`$Name, `$CommandType, `$ErrorAction) if (`$Name -ne 'wsl.exe') { throw 'Unexpected executable resolution' }; [IO.File]::WriteAllText('" + $resolverPath.Replace("'", "''") + "','resolved'); [pscustomobject]@{ Source='" + $fixtureExe.Replace("'", "''") + "' } }"
    $source = $launcherText.Substring(0, $admission.Extent.StartOffset) + $stub + $launcherText.Substring($admission.Extent.EndOffset)
    $ready = if ($case.ContainsKey('ReadinessDenied')) { '$false' } else { '$true' }
    $readyStub = "function Wait-CreLocalApi { [IO.File]::WriteAllText('" + $readinessPath.Replace("'", "''") + "','probed'); return [ordered]@{ ready = $ready; scope = 'synthetic' } }"
    $source = $source.Replace($readiness.Extent.Text, $readyStub)
    if ($case.ContainsKey('AuditFailure')) {
        $source = $source.Replace('    $temporary = $Path +', "    if (`$Record.status -ceq 'startup_admission_waiting') { throw 'Synthetic waiting audit failure' }`n    `$temporary = `$Path +")
    }
    $scriptFile = Join-Path $caseWindows 'Invoke-CreGuardedTier.ps1'
    [IO.File]::WriteAllText($scriptFile, $source, (New-Object Text.UTF8Encoding($true)))
    $result = [CreWindowsCapture.Capture]::Run($hostExecutable, (@('-NoProfile', '-NonInteractive', '-File', $scriptFile) + $case.Args), (Join-Path $caseRoot 'stdout'), (Join-Path $caseRoot 'stderr'), $null)
    Assert-True (-not $result.Failed -and $result.NativeExitCode -eq $case.Exit) ("Unexpected host result: " + $case.Name)
    Assert-True ([IO.File]::Exists($argvPath) -eq $case.Dispatch) ("Unexpected dispatch: " + $case.Name)
    if ($case.ContainsKey('CpuRecovery')) {
        $sampleCount = [int][IO.File]::ReadAllText($admissionPath)
        $expectedSamples = if ($case.ContainsKey('AuditFailure')) { 1 } else { 2 }
        Assert-True ($sampleCount -eq $expectedSamples) 'CPU recovery must not retry after dispatch or failed audit.'
    }
    if (-not $case.Dispatch) {
        Assert-True (-not [IO.File]::Exists($resolverPath)) ("Refusal/CheckOnly must precede WSL resolution: " + $case.Name)
        if ($case.ContainsKey('Check')) { Assert-True (-not [IO.Directory]::Exists((Join-Path $caseRoot 'out'))) 'CheckOnly writes no audit/log files.' }
        if ($case.ContainsKey('Check')) { Assert-True (-not [IO.File]::Exists($readinessPath)) 'CheckOnly must never probe the API.' }
        if (-not $case.ContainsKey('Check') -and -not $case.ContainsKey('Denied') -and -not $case.ContainsKey('ReadinessDenied')) {
            Assert-True (-not [IO.File]::Exists($readinessPath) -and -not [IO.File]::Exists($admissionPath)) 'Invalid options must refuse before readiness or resource sampling.'
        }
        if ($case.ContainsKey('ReadinessDenied')) {
            Assert-True (-not [IO.File]::Exists($admissionPath)) 'Unavailable API must refuse before CPU sampling.'
            $failedAudit = [IO.File]::ReadAllText(@(Get-ChildItem (Join-Path $caseRoot 'out/windows-launcher') -Filter '*.json')[0].FullName) | ConvertFrom-Json
            Assert-True ($failedAudit.status -ceq 'local_api_not_ready_no_dispatch' -and $failedAudit.dispatch -ceq 'not_attempted' -and -not $failedAudit.localApiReadiness.ready -and $null -eq $failedAudit.nativeExitCode) 'Readiness refusal must leave a clear no-dispatch audit.'
        }
        continue
    }
    Assert-True ([IO.File]::Exists($readinessPath) -and [IO.File]::Exists($admissionPath)) 'Dispatch requires readiness followed by fresh resource admission.'
    $admissionAudit = [IO.File]::ReadAllText(@(Get-ChildItem (Join-Path $caseRoot 'out/windows-launcher') -Filter '*.json')[0].FullName) | ConvertFrom-Json
    $createdAt = if ($admissionAudit.createdAt -is [DateTime]) { [DateTimeOffset]::new($admissionAudit.createdAt) } else { [DateTimeOffset]::Parse($admissionAudit.createdAt) }
    Assert-True ($admissionAudit.windowsAdmissionUnixMs -ge $createdAt.ToUnixTimeMilliseconds()) 'Admission timestamp is generated only after startup admission.'
    Assert-True ($admissionAudit.startupAdmission.reason -ceq 'admitted') 'Dispatched audit must retain qualified startup admission.'
    $reader = New-Object IO.BinaryReader([IO.File]::OpenRead($argvPath), (New-Object Text.UTF8Encoding($false)))
    try { $count = $reader.ReadInt32(); $forwarded = @(for ($i = 0; $i -lt $count; $i++) { $reader.ReadString() }) } finally { $reader.Dispose() }
    Assert-True (($forwarded[0..5] -join '|') -ceq '-d|Ubuntu-24.04|-u|cayman|--exec|env') 'Fixed WSL executable boundary must remain separate argv.'
    Assert-True ($forwarded -contains '--windows-admission-unix-ms' -and $forwarded -contains $sha) 'Reviewed SHA and startup admission must be forwarded.'
    if ($case.Name -in @('monitor', 'enrich')) {
        Assert-True ($forwarded -contains '/home/cayman/code/firecrawl/scripts/firecrawl-ops/cre_collector/cre_guarded_tier.py' -and -not ($forwarded -contains '--work-deadline-seconds')) 'Incremental worker must be unchanged.'
        if ($case.Name -eq 'monitor') { Assert-True (($forwarded -join '|').Contains('monitor|--source|hanley|--expected-sha|' + $sha + '|--batch|50|--deadline-seconds|1800|')) 'Default monitor argv must remain unchanged.' }
        else { Assert-True ($forwarded -contains '--apply' -and ($forwarded -join '|').Contains('--batch|3|--deadline-seconds|90')) 'Enrich apply/batch/deadline must remain meaningful.' }
        $expected = @('-d', 'Ubuntu-24.04', '-u', 'cayman', '--exec', 'env', 'FIRECRAWL_API_URL=http://127.0.0.1:3002', 'python3', '/home/cayman/code/firecrawl/scripts/firecrawl-ops/cre_collector/cre_guarded_tier.py', $case.Name, '--source', 'hanley', '--expected-sha', $sha, '--batch')
        $expected += if ($case.Name -eq 'monitor') { @('50', '--deadline-seconds', '1800') } else { @('3', '--deadline-seconds', '90') }
        $expected += @('--env-file', '/home/cayman/.config/cre/equire.env', '--windows-admission-unix-ms', $forwarded[21])
        if ($case.Name -eq 'enrich') { $expected += '--apply' }
        Assert-True (($forwarded -join '|') -ceq ($expected -join '|')) 'Every incremental argument must remain byte-compatible except its current timestamp.'
    } else {
        $expectedRepo = if ($case.ContainsKey('LinuxRepo')) { $case.LinuxRepo } else { '/home/cayman/code/firecrawl' }
        foreach ($expected in @('JLL_DETAIL_CONCURRENCY=1', 'JLL_INVESTOR_DETAIL_CONCURRENCY=1', 'CUSHMAN_DETAIL_CONCURRENCY=1', 'COLLIERS_MAIN_COVEO_ENABLE=1', 'COLLIERS_MAIN_DETAIL_CONCURRENCY=1', 'COLLIERS_MAIN_DETAIL_START_INTERVAL_MS=3000', 'COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS=60000', 'NAI_ENUMERATION_CONCURRENCY=1', 'python3', ($expectedRepo + '/scripts/firecrawl-ops/cre_collector/cre_checkpoint_series.py'))) {
            Assert-True ($forwarded -contains $expected) ('Missing conservative profile argv: ' + $expected)
        }
        $resolvedConcurrency = if ($case.ContainsKey('Concurrency')) { [string]$case.Concurrency } else { '1' }
        Assert-True (($forwarded -join '|').Contains('--concurrency|' + $resolvedConcurrency + '|--nice|15|--page-cap|400|--attempts-per-source|3|--max-resource-recoveries-per-source|0|--max-host-cpu-percent|90|--cpu-sustain-seconds|30|--cpu-sample-seconds|2|--memory-sustain-seconds|20|--memory-sample-seconds|2|')) 'Series page/retry and CPU/memory guards must be explicit without resource replay.'
        Assert-True (-not ($forwarded -contains '/usr/bin/nice') -and ($forwarded -join '|').Contains('NAI_ENUMERATION_CONCURRENCY=1|python3|')) 'Coordinator must inherit caller priority; canonical --nice supplies the only checkpoint adjustment.'
        $selectedSource = if ($case.Name -eq 'savills-hold') { 'savills' } elseif ($case.ContainsKey('Workers')) { 'cbre,jll' } elseif ($case.Name -eq 'essex-current') { 'essex-realty' } elseif ($case.Name -eq 'pyramid-current') { 'pyramid-brokerage' } elseif ($case.Name -eq 'interra-current') { 'interra-realty' } elseif ($case.Name -eq 'buildout-current') { 'faris-lee' } elseif ($case.Name -in @('selected', 'max-budget')) { 'hanley' } else { 'all' }
        $budget = if ($case.Name -eq 'savills-hold') { '30' } elseif ($case.ContainsKey('Workers')) { '30' } elseif ($case.Name -in @('selected', 'interra-current', 'buildout-current', 'pyramid-current', 'essex-current')) { '30' } elseif ($case.Name -eq 'max-budget') { '86400' } else { '3600' }
        Assert-True (($forwarded -join '|').Contains('--sources|' + $selectedSource + '|') -and ($forwarded -join '|').Contains('--work-deadline-seconds|' + $budget + '|')) 'Selected source and exact finite work budget must be forwarded.'
        if ($case.ContainsKey('Workers')) {
            Assert-True (($forwarded -join '|').Contains('--pipeline|--source-workers|' + $case.Workers)) 'Resolved worker setting crosses actual native argv boundary.'
        }
        if ($case.Name -in @('interra-current', 'buildout-current', 'pyramid-current', 'essex-current')) {
            Assert-True (($forwarded -join '|').EndsWith('--admit-current-inventory-source|' + $selectedSource)) 'Explicit source admission must survive the actual native argv boundary.'
        } else {
            Assert-True (-not ($forwarded -contains '--admit-current-inventory-source')) 'Default runs must not acquire current-inventory admission.'
        }
        Assert-True (($forwarded -contains '--admit-baseline-hold-additively') -eq ($case.Name -eq 'savills-hold')) 'Only explicit Savills admission crosses the native boundary.'
        if ($case.Name -eq 'quoted-paths') { Assert-True ($forwarded -contains $quotedEnv -and $forwarded -contains ($quotedRepo + '/scripts/firecrawl-ops/cre_collector/cre_checkpoint_series.py')) 'Spaces and literal quotes must survive the native argv boundary without a shell.' }
        Assert-True (-not ($forwarded -contains '--apply') -and -not ($forwarded -contains '--batch') -and -not ($forwarded -contains '--resume')) 'Series has no incremental apply/batch or automatic replay.'
        $audit = @(Get-ChildItem (Join-Path $caseRoot 'out/windows-launcher') -Filter '*.json')[0]
        $record = [IO.File]::ReadAllText($audit.FullName) | ConvertFrom-Json
        Assert-True ($record.acknowledgeDatabaseWrites -and $record.workDeadlineSeconds -ge 30 -and $record.linuxSettlement -ceq 'unverified') 'Series audit describes write acknowledgement and preserves settlement uncertainty.'
    }
}

$stop = Join-Path $PSScriptRoot 'Stop-CreCheckpointSeries.ps1'
$tokens = $null; $errors = $null
$stopText = [IO.File]::ReadAllText($stop)
[void][Management.Automation.Language.Parser]::ParseInput($stopText, [ref]$tokens, [ref]$errors)
Assert-True ($errors.Count -eq 0) 'Stop wrapper must parse.'
# Static option agreement only; never import or execute Python on Windows.
$seriesPython = [IO.File]::ReadAllText((Join-Path $PSScriptRoot '../cre_checkpoint_series.py'))
foreach ($option in @('--sources', '--concurrency', '--nice', '--page-cap', '--attempts-per-source', '--max-resource-recoveries-per-source', '--max-host-cpu-percent', '--cpu-sustain-seconds', '--cpu-sample-seconds', '--memory-sustain-seconds', '--memory-sample-seconds', '--expected-sha', '--windows-admission-unix-ms', '--work-deadline-seconds', '--env-file')) {
    Assert-True ([regex]::IsMatch($seriesPython, ('parser\.add_argument\(\s*"' + [regex]::Escape($option) + '"'))) ('Canonical Python series option must exist: ' + $option)
}
$stopPython = [IO.File]::ReadAllText((Join-Path $PSScriptRoot '../cre_stop_checkpoint_series.py'))
foreach ($option in @('--series-dir', '--invocation-id', '--wait-seconds')) {
    Assert-True ([regex]::IsMatch($stopPython, ('parser\.add_argument\(\s*"' + [regex]::Escape($option) + '"'))) ('Canonical Python stop option must exist: ' + $option)
}
$stopSource = $stopText.Replace('Set-StrictMode -Version Latest', ("function Get-Command { throw 'CheckOnly must not resolve WSL' }`nSet-StrictMode -Version Latest"))
$stopTest = Join-Path $ProofDirectory 'stop-check.ps1'
[IO.File]::WriteAllText($stopTest, $stopSource, (New-Object Text.UTF8Encoding($true)))
$seriesDir = '/home/cayman/code/firecrawl/scripts/firecrawl-ops/cre_collector/out/checkpoint-series/2026-10-14T010203Z-abcdef123456'
$result = [CreWindowsCapture.Capture]::Run($hostExecutable, @('-NoProfile', '-NonInteractive', '-File', $stopTest, '-SeriesDirectory', $seriesDir, '-InvocationId', ('b' * 32), '-WaitSeconds', '120', '-CheckOnly'), (Join-Path $ProofDirectory 'stop.stdout'), (Join-Path $ProofDirectory 'stop.stderr'), $null)
Assert-True (-not $result.Failed -and $result.NativeExitCode -eq 0) 'Exact stop CheckOnly must return without WSL.'
$preview = [IO.File]::ReadAllText((Join-Path $ProofDirectory 'stop.stdout')) | ConvertFrom-Json
Assert-True (-not $preview.ownerValidated -and -not $preview.signalSent -and $preview.arguments -contains $seriesDir -and $preview.arguments -contains ('b' * 32) -and $preview.arguments -contains '120') 'Stop preview must preserve exact identity without claiming authority.'
foreach ($exitCode in @(0, 1, 2)) {
    $stopDirectory = if ($exitCode -eq 2) { '/tmp/synthetic space/quote''s series' } else { $seriesDir }
    $env:CRE_TEST_ARGV = Join-Path $ProofDirectory ("stop-$exitCode.argv.bin")
    $env:CRE_TEST_EXIT = "$exitCode"
    $resolver = "function Get-Command { param(`$Name, `$CommandType, `$ErrorAction) [pscustomobject]@{ Source='" + $fixtureExe.Replace("'", "''") + "' } }`n"
    $stopSource = $stopText.Replace('Set-StrictMode -Version Latest', ($resolver + 'Set-StrictMode -Version Latest'))
    $stopTest = Join-Path $ProofDirectory ("stop-$exitCode.ps1")
    [IO.File]::WriteAllText($stopTest, $stopSource, (New-Object Text.UTF8Encoding($true)))
    $result = [CreWindowsCapture.Capture]::Run($hostExecutable, @('-NoProfile', '-NonInteractive', '-File', $stopTest, '-SeriesDirectory', $stopDirectory, '-InvocationId', ('b' * 32)), (Join-Path $ProofDirectory "stop-$exitCode.stdout"), (Join-Path $ProofDirectory "stop-$exitCode.stderr"), $null)
    Assert-True (-not $result.Failed -and $result.NativeExitCode -eq $exitCode) 'Stop must preserve Linux success/refusal/unknown exits without a Windows CPU gate.'
    $reader = New-Object IO.BinaryReader([IO.File]::OpenRead($env:CRE_TEST_ARGV), (New-Object Text.UTF8Encoding($false)))
    try { $count = $reader.ReadInt32(); $forwarded = @(for ($i = 0; $i -lt $count; $i++) { $reader.ReadString() }) } finally { $reader.Dispose() }
    Assert-True (($forwarded -join '|') -ceq ('-d|Ubuntu-24.04|-u|cayman|--exec|python3|/home/cayman/code/firecrawl/scripts/firecrawl-ops/cre_collector/cre_stop_checkpoint_series.py|--series-dir|' + $stopDirectory + '|--invocation-id|' + ('b' * 32) + '|--wait-seconds|60')) 'Thin stop must forward exact owner identity and default bounded wait.'
}
foreach ($invalid in @(@('-SeriesDirectory', 'relative/path', '-InvocationId', ('b' * 32)), @('-SeriesDirectory', '/tmp/"ambiguous"', '-InvocationId', ('b' * 32)), @('-SeriesDirectory', $seriesDir, '-InvocationId', ('b' * 32), '-LinuxRepo', '/tmp/"ambiguous"'), @('-SeriesDirectory', $seriesDir, '-InvocationId', 'latest'), @('-SeriesDirectory', $seriesDir, '-InvocationId', ('b' * 32), '-WaitSeconds', '121'))) {
    $result = [CreWindowsCapture.Capture]::Run($hostExecutable, (@('-NoProfile', '-NonInteractive', '-File', (Join-Path $ProofDirectory 'stop-check.ps1'), '-CheckOnly') + $invalid), (Join-Path $ProofDirectory ([Guid]::NewGuid().ToString('N') + '.stdout')), (Join-Path $ProofDirectory ([Guid]::NewGuid().ToString('N') + '.stderr')), $null)
    Assert-True (-not $result.Failed -and $result.NativeExitCode -ne 0) 'Malformed exact stop identity/wait must refuse without WSL.'
}
Remove-Item Env:CRE_TEST_ARGV, Env:CRE_TEST_EXIT
Write-Output "Windows checkpoint-series launcher PASS: $script:checks assertions; proof=$ProofDirectory"
