<# Actual summary reader/formatter functions with a synthetic WSL executable.
   No WSL, provider, database, scheduler, credential or network access. #>
[CmdletBinding()]
param([string]$ProofDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $ProofDirectory) { $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/readable-' + [Guid]::NewGuid().ToString('N')) }
[void](New-Item -ItemType Directory -Path $ProofDirectory -Force)
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$script:checks = 0
function Assert-Cre($condition, [string]$message) { if (-not $condition) { throw $message }; $script:checks++ }
$sourceText = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($sourceText,[ref]$tokens,[ref]$errors)
Assert-Cre ($errors.Count -eq 0) 'Shipped launcher must parse.'
foreach ($name in @('Get-CreBrokerageName','Get-CreVerifiedSummary','Get-CreNotificationText')) {
    $definition=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true))
    Assert-Cre ($definition.Count -eq 1) "Exactly one shipped $name function is required."
    . ([scriptblock]::Create($definition[0].Extent.Text))
}
Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'CreNativeCapture.cs')))
$fixtureSource=@'
using System;
using System.IO;
public static class SummaryFixture {
    public static int Main(string[] args) {
        File.WriteAllLines(Environment.GetEnvironmentVariable("CRE_TEST_SUMMARY_ARGV"), args);
        File.WriteAllText(Environment.GetEnvironmentVariable("CRE_TEST_SUMMARY_ARGV") + ".pid", System.Diagnostics.Process.GetCurrentProcess().Id.ToString());
        if (Environment.GetEnvironmentVariable("CRE_TEST_SUMMARY_MODE") == "delay") System.Threading.Thread.Sleep(300);
        Console.Write(File.ReadAllText(Environment.GetEnvironmentVariable("CRE_TEST_SUMMARY_JSON")));
        if (Environment.GetEnvironmentVariable("CRE_TEST_SUMMARY_MODE") == "hang") System.Threading.Thread.Sleep(15000);
        return 0;
    }
}
'@
$fixtureCs=Join-Path $ProofDirectory 'summary.cs'
$fixtureExe=Join-Path $ProofDirectory 'summary.exe'
[IO.File]::WriteAllText($fixtureCs,$fixtureSource)
$compiler=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:exe "/out:$fixtureExe" $fixtureCs
if ($LASTEXITCODE -ne 0) { throw 'Synthetic reader compilation failed.' }
function Get-Command { param($Name,$CommandType,$ErrorAction) if ($Name -ne 'wsl.exe') { throw 'Unexpected executable lookup.' }; [pscustomobject]@{Source=$fixtureExe} }
$Tier='series'; $Source='newmark'; $ExpectedSha=('a'*40)
$LinuxRepo='/home/fixture/firecrawl with spaces'; $Distro='fixture'; $LinuxUser='fixture'
$auditDir=$ProofDirectory
$seriesDirectory=$LinuxRepo+'/scripts/firecrawl-ops/cre_collector/out/checkpoint-series/2026-10-15T172000Z'
$summaryJson=Join-Path $ProofDirectory 'response.json'
$argvFile=Join-Path $ProofDirectory 'argv.txt'
$env:CRE_TEST_SUMMARY_JSON=$summaryJson
$env:CRE_TEST_SUMMARY_ARGV=$argvFile
function New-Record {
    $capture=New-Object CreWindowsCapture.CaptureResult
    $capture.Stdout.ObservedRunDirectory=$seriesDirectory
    $capture.NativeExitCode=0
    return [ordered]@{createdAt=[DateTimeOffset]::UtcNow.AddSeconds(-5).ToString('o');status='returned';nativeExitCode=0;
        windowsAdmissionUnixMs=[long]1792080000000;capture=$capture}
}
function New-Summary {
    return [ordered]@{schema_version=1;verified=$true;source=$Source;collector_git_sha=$ExpectedSha;
        windows_admission_unix_ms=[long]1792080000000;series_id='2026-10-15T172000Z';invocation_id=('b'*32);
        run_id='2026-10-15T172001Z-123456abcdef';verified_listings=4535;transaction_scope=@('sale','lease');
        measurement='generation_exact_current_inventory';quality_no_regression=$true;local_settlement='confirmed'}
}
function Save-Summary($value) { [IO.File]::WriteAllText($summaryJson,($value|ConvertTo-Json -Compress -Depth 8),(New-Object Text.UTF8Encoding($false))) }
Save-Summary (New-Summary)
$record=New-Record
$result=Get-CreVerifiedSummary $record 'positive'
Assert-Cre ($result.verified_listings -eq 4535 -and $record.Contains('notificationSummary')) 'Only qualified helper evidence supplies counts.'
$argv=[IO.File]::ReadAllLines($argvFile)
Assert-Cre ($argv -contains $seriesDirectory -and $argv -contains $ExpectedSha -and $argv -contains '1792080000000') 'Reader argv must bind exact locator, SHA and Windows attempt admission.'
Assert-Cre ($argv -contains '10s' -and $argv -contains '--kill-after=2s' -and $argv -notcontains '--env-file') 'Read-only helper is finite and never receives database credentials.'
Assert-Cre ($argv -contains ($LinuxRepo+'/scripts/firecrawl-ops/cre_collector/cre_notification_summary.py')) 'Actual CRT quoting preserves a Linux checkout path containing spaces.'
$start=Get-CreNotificationText $record 'Start' 0 $null
$finish=Get-CreNotificationText $record 'Terminal' 0 $result
Assert-Cre ($start.Contains('Newmark refresh started.') -and $start.Contains('collection has not started')) 'Start must name the brokerage without claiming collection has happened.'
Assert-Cre ($finish.Contains('4,535 listings verified.') -and $finish.Contains('Sale and lease inventory checked.')) 'Terminal message must label verified generation inventory and truthful transaction scope.'
Assert-Cre ($finish -match 'completed in [0-9]+s' -and $finish.Contains('no quality regression')) 'Summary must include measured attempt duration and quality outcome.'
Assert-Cre ($finish -notmatch 'wrapper_exit|native_exit|[a-f0-9]{32}|updated|new listings|whole.source|full.pipeline') 'Channel text must omit technical IDs and unmeasured or broader claims.'
Assert-Cre ([Text.Encoding]::UTF8.GetByteCount((@{text=$finish}|ConvertTo-Json -Compress)) -lt 4096) 'Full JSON payload stays below the unchanged sender bound.'
$env:CRE_TEST_SUMMARY_MODE='hang'
$timed=New-Record
$clock=[Diagnostics.Stopwatch]::StartNew()
$hung=Get-CreVerifiedSummary $timed 'hung-reader' -ReadDeadlineMilliseconds 500
$clock.Stop()
Assert-Cre ($null -eq $hung -and $timed.notificationSummaryCapture.ReadOnlyDeadlineExpired -and $timed.notificationSummaryCapture.Failed) 'Even valid JSON emitted before a hung read-only frontend cannot supply counts.'
Assert-Cre ($clock.Elapsed.TotalSeconds -lt 8) 'Independent native deadline must bound a hung frontend and its stream drains.'
$fixturePid=[int][IO.File]::ReadAllText($argvFile+'.pid')
Assert-Cre ($null -eq (Microsoft.PowerShell.Management\Get-Process -Id $fixturePid -ErrorAction SilentlyContinue)) 'Only the owned synthetic read-only frontend must exit after its deadline.'
Assert-Cre ($timed.nativeExitCode -eq 0 -and (Get-CreNotificationText $timed 'Terminal' 0 $hung) -notmatch 'listings verified|completed') 'Summary timeout must preserve the original pipeline exit and use the no-count fallback.'
$env:CRE_TEST_SUMMARY_MODE='delay'
$foregroundClock=[Diagnostics.Stopwatch]::StartNew()
$foreground=[CreWindowsCapture.Capture]::Run($fixtureExe,@(),(Join-Path $ProofDirectory 'foreground.stdout'),(Join-Path $ProofDirectory 'foreground.stderr'),$null)
$foregroundClock.Stop()
Assert-Cre (-not $foreground.Failed -and -not $foreground.ReadOnlyDeadlineExpired -and $foreground.NativeExitCode -eq 0 -and $foregroundClock.ElapsedMilliseconds -ge 300) 'Ordinary foreground capture retains its original wait and never uses the optional reader deadline.'
Remove-Item Env:CRE_TEST_SUMMARY_MODE
foreach ($field in @('verified','source','collector_git_sha','windows_admission_unix_ms','series_id','invocation_id','run_id','verified_listings','measurement','quality_no_regression','local_settlement','transaction_scope')) {
    $invalid=New-Summary
    $invalid[$field]=switch($field){'verified'{ 'true' };'verified_listings'{ 0 };'quality_no_regression'{ 'true' };'transaction_scope'{ @('sale') };default{ 'mismatch' }}
    Save-Summary $invalid
    $candidate=$null
    try {$candidate=Get-CreVerifiedSummary (New-Record) ('bad-'+$field)} catch { }
    Assert-Cre ($null -eq $candidate) "Wrong $field must never reach a verified summary."
}
Save-Summary (New-Summary)
foreach ($mode in @('stdout-conflict','stderr-conflict','cross-stream','escape','wrong-prefix','failed','native-nonzero','monitor','missing-admission')) {
    $record=New-Record
    switch($mode){
        'stdout-conflict'{$record.capture.Stdout.RunReferenceConflict=$true}
        'stderr-conflict'{$record.capture.Stderr.RunReferenceConflict=$true}
        'cross-stream'{$record.capture.Stderr.ObservedRunDirectory=$seriesDirectory+'other'}
        'escape'{$record.capture.Stdout.ObservedRunDirectory=$seriesDirectory+'/../elsewhere'}
        'wrong-prefix'{$record.capture.Stdout.ObservedRunDirectory='/tmp/'+$seriesDirectory.Split('/')[-1]}
        'failed'{$record.status='windows_capture_failed_linux_settlement_unverified'}
        'native-nonzero'{$record.nativeExitCode=23}
        'monitor'{$Tier='monitor'}
        'missing-admission'{$record.Remove('windowsAdmissionUnixMs')}
    }
    if([IO.File]::Exists($argvFile)){[IO.File]::Delete($argvFile)}
    $candidate=Get-CreVerifiedSummary $record ('blocked-'+$mode)
    Assert-Cre ($null -eq $candidate -and -not [IO.File]::Exists($argvFile)) "$mode must refuse before any helper dispatch."
    $Tier='series'
}
$record=New-Record
$unknown=Get-CreNotificationText $record 'Terminal' 0 $null
Assert-Cre ($unknown.Contains('could not be verified') -and $unknown -notmatch 'listings verified|completed') 'Wrapper zero alone must not claim a refresh or listing count.'
$failure=Get-CreNotificationText $record 'Terminal' 23 (New-Summary)
Assert-Cre ($failure.Contains('needs attention') -and $failure -notmatch 'completed|listings verified') 'A nonzero result must not be promoted by available metrics.'
$record.status='windows_terminal_audit_failed_linux_settlement_unverified'
Assert-Cre ((Get-CreNotificationText $record 'Terminal' 1 $null).Contains('audit could not be saved')) 'Audit failure needs a clear completion warning.'
$Source='cushman-wakefield'
Assert-Cre ((Get-CreNotificationText $record 'Start' 0 $null).Contains('Cushman &amp; Wakefield')) 'Slack control characters must be escaped.'
$Source='<!channel>'
Assert-Cre ((Get-CreNotificationText $record 'Start' 0 $null) -notmatch '<!|@') 'Unrecognized source input cannot become a mention.'
Remove-Item Env:CRE_TEST_SUMMARY_JSON,Env:CRE_TEST_SUMMARY_ARGV
Write-Output "Readable CRE notification PASS: $script:checks assertions; proof=$ProofDirectory"
