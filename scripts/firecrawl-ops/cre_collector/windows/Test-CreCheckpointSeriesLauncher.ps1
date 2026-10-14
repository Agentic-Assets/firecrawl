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
$sha = 'a' * 40
$series = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '3600', '-AcknowledgeDatabaseWrites')
$quotedRepo = '/home/synthetic space/quote''s "directory"/firecrawl'
$quotedEnv = '/tmp/synthetic space/quote''s "env".env'
$cases = @(
    @{ Name = 'series'; Args = $series; Exit = 0; Dispatch = $true },
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
    @{ Name = 'denied'; Args = $series; Exit = 1; Dispatch = $false; Denied = $true },
    @{ Name = 'bad-budget'; Args = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '29', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'too-large-budget'; Args = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '86401', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'source-list'; Args = @('-Tier', 'series', '-Source', 'jll,hanley', '-ExpectedSha', $sha, '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false },
    @{ Name = 'bad-sha'; Args = @('-Tier', 'series', '-Source', 'all', '-ExpectedSha', 'main', '-WorkDeadlineSeconds', '30', '-AcknowledgeDatabaseWrites'); Exit = 1; Dispatch = $false }
)
foreach ($case in $cases) {
    $caseRoot = Join-Path $ProofDirectory $case.Name
    $caseWindows = Join-Path $caseRoot 'windows'
    New-Item -ItemType Directory -Path $caseWindows -Force | Out-Null
    [IO.File]::Copy((Join-Path $PSScriptRoot 'CreNativeCapture.cs'), (Join-Path $caseWindows 'CreNativeCapture.cs'))
    $argvPath = Join-Path $caseRoot 'argv.bin'
    $resolverPath = Join-Path $caseRoot 'resolved'
    $env:CRE_TEST_ARGV = $argvPath
    $env:CRE_TEST_EXIT = if ($case.ContainsKey('Native')) { $case.Native } else { '0' }
    $admitted = if ($case.ContainsKey('Denied')) { '$false' } else { '$true' }
    $stub = "function Get-CreAdmission { return [ordered]@{ admitted = $admitted; scope = 'synthetic' } }"
    $stub += "`nfunction Get-Command { param(`$Name, `$CommandType, `$ErrorAction) if (`$Name -ne 'wsl.exe') { throw 'Unexpected executable resolution' }; [IO.File]::WriteAllText('" + $resolverPath.Replace("'", "''") + "','resolved'); [pscustomobject]@{ Source='" + $fixtureExe.Replace("'", "''") + "' } }"
    $source = $launcherText.Substring(0, $admission.Extent.StartOffset) + $stub + $launcherText.Substring($admission.Extent.EndOffset)
    $scriptFile = Join-Path $caseWindows 'Invoke-CreGuardedTier.ps1'
    [IO.File]::WriteAllText($scriptFile, $source, (New-Object Text.UTF8Encoding($true)))
    $result = [CreWindowsCapture.Capture]::Run($hostExecutable, (@('-NoProfile', '-NonInteractive', '-File', $scriptFile) + $case.Args), (Join-Path $caseRoot 'stdout'), (Join-Path $caseRoot 'stderr'), $null)
    Assert-True (-not $result.Failed -and $result.NativeExitCode -eq $case.Exit) ("Unexpected host result: " + $case.Name)
    Assert-True ([IO.File]::Exists($argvPath) -eq $case.Dispatch) ("Unexpected dispatch: " + $case.Name)
    if (-not $case.Dispatch) {
        Assert-True (-not [IO.File]::Exists($resolverPath)) ("Refusal/CheckOnly must precede WSL resolution: " + $case.Name)
        if ($case.ContainsKey('Check')) { Assert-True (-not [IO.Directory]::Exists((Join-Path $caseRoot 'out'))) 'CheckOnly writes no audit/log files.' }
        continue
    }
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
        Assert-True (($forwarded -join '|').Contains('--concurrency|1|--nice|15|--page-cap|400|--attempts-per-source|3|--max-resource-recoveries-per-source|0|--max-host-cpu-percent|90|--cpu-sustain-seconds|30|--cpu-sample-seconds|2|--memory-sustain-seconds|20|--memory-sample-seconds|2|')) 'Series page/retry and CPU/memory guards must be explicit without resource replay.'
        Assert-True (-not ($forwarded -contains '/usr/bin/nice') -and ($forwarded -join '|').Contains('NAI_ENUMERATION_CONCURRENCY=1|python3|')) 'Coordinator must inherit caller priority; canonical --nice supplies the only checkpoint adjustment.'
        $selectedSource = if ($case.Name -in @('selected', 'max-budget')) { 'hanley' } else { 'all' }
        $budget = if ($case.Name -eq 'selected') { '30' } elseif ($case.Name -eq 'max-budget') { '86400' } else { '3600' }
        Assert-True (($forwarded -join '|').Contains('--sources|' + $selectedSource + '|') -and ($forwarded -join '|').Contains('--work-deadline-seconds|' + $budget + '|')) 'Selected source and exact finite work budget must be forwarded.'
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
