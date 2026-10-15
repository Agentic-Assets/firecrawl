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
        transaction_breakdown=[ordered]@{sale=3000;lease=2000;both=465;measurement='current_staged_inventory_transaction_types'};
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
$rocket=[char]::ConvertFromUtf32(0x1F680); $successMark=[char]::ConvertFromUtf32(0x2705); $warningMark=[char]::ConvertFromUtf32(0x26A0)+[char]0xFE0F; $bullet=[char]0x2022
Assert-Cre ($start.StartsWith("$rocket *Newmark | Started*") -and $start.Contains('collection has not started')) 'Start must begin with its emoji and bold brokerage heading without claiming collection has happened.'
Assert-Cre ($finish.StartsWith("$successMark *Newmark | Completed*") -and $finish.Contains('*Listings verified:* 4,535 unique properties')) 'Terminal message must lead with status and label the verified generation total.'
Assert-Cre ($finish.Contains('*For sale:* 3,000 | *For lease:* 2,000') -and $finish.Contains('*Both:* 465 (included in For sale and For lease)')) 'Transaction counts use the exact staged identity union and disclose overlap.'
Assert-Cre ($finish.Contains('*Database readback:* passed; no quality regression.') -and $finish -match "$bullet \*Duration:\* [0-9]+[smh]") 'Summary includes the measured duration and exact readback outcome.'
Assert-Cre ($finish -notmatch 'wrapper_exit|native_exit|[a-f0-9]{32}|updated|new listings|whole.source|full.pipeline|&amp;') 'Channel text must omit technical IDs and unmeasured or broader claims.'
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
$optionalBreakdownFailures=@(
    [ordered]@{sale=0;lease=0;both=0;measurement='wrong'},
    [ordered]@{sale=3000;lease=2000;both=464;measurement='current_staged_inventory_transaction_types'},
    [ordered]@{sale=-1;lease=2001;both=465;measurement='current_staged_inventory_transaction_types'},
    [ordered]@{sale=3000;lease=2000;both=2001;measurement='current_staged_inventory_transaction_types'},
    [ordered]@{sale=3000;lease=2000;both=465},
    [ordered]@{lease=4535;both=0;measurement='current_staged_inventory_transaction_types'}
)
$breakdownIndex=0
foreach($invalidBreakdown in $optionalBreakdownFailures){
    $breakdownIndex++
    $invalid=New-Summary; $invalid.transaction_breakdown=$invalidBreakdown; Save-Summary $invalid
    $candidate=Get-CreVerifiedSummary (New-Record) ('bad-breakdown-'+$breakdownIndex)
    Assert-Cre ($null -ne $candidate -and $candidate.verified_listings -eq 4535 -and $null -eq $candidate.transaction_breakdown) 'Invalid optional breakdown must not discard the independently verified total.'
    Assert-Cre ((Get-CreNotificationText (New-Record) 'Terminal' 0 $candidate).Contains('For sale:* unavailable')) 'Invalid optional breakdown is reported unavailable while preserving the verified total.'
}
$withoutBreakdown=New-Summary; $withoutBreakdown.Remove('transaction_breakdown'); Save-Summary $withoutBreakdown
$legacySummary=Get-CreVerifiedSummary (New-Record) 'without-breakdown'
Assert-Cre ($null -ne $legacySummary -and $legacySummary.verified_listings -eq 4535) 'Older schema-v1 summaries without optional transaction counts remain compatible.'
Assert-Cre ((Get-CreNotificationText (New-Record) 'Terminal' 0 $legacySummary).Contains('For sale:* unavailable')) 'Missing optional counts are clearly unavailable.'
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
Assert-Cre ($unknown -match 'could not be verified' -and $unknown -notmatch 'listings verified|completed') 'Wrapper zero alone must not claim a refresh or listing count.'
$failure=Get-CreNotificationText $record 'Terminal' 23 (New-Summary)
Assert-Cre ($failure.StartsWith("$warningMark *Newmark | Needs attention*") -and $failure -notmatch 'Completed|Listings verified') 'A nonzero result must not be promoted by available metrics.'
$record.status='windows_terminal_audit_failed_linux_settlement_unverified'
Assert-Cre ((Get-CreNotificationText $record 'Terminal' 1 $null).Contains('audit could not be saved')) 'Audit failure needs a clear completion warning.'
$Source='cushman-wakefield'
Assert-Cre ((Get-CreNotificationText $record 'Start' 0 $null).Contains("*Cushman &amp; Wakefield | Started*")) 'Known brokerage labels preserve Slack entity escaping.'
$Source='nai-hallmark'
Assert-Cre ((Get-CreBrokerageName $Source) -ceq 'NAI Hallmark') 'Generic broker names preserve NAI capitalization.'
$Source='cbre-other'
Assert-Cre ((Get-CreBrokerageName $Source) -ceq 'CBRE Other') 'Generic source labels preserve allowed CBRE capitalization.'
$Source='<!channel>'
Assert-Cre ((Get-CreNotificationText $record 'Start' 0 $null) -notmatch '<!|@') 'Unrecognized source input cannot become a mention.'
$Pipeline = $true
$selectedSourceKeys = @('newmark','cbre','jll')
$selectedSourceArgument = $selectedSourceKeys -join ','
$pipelineSummary = New-Summary
$pipelineSummary['selected_source_keys'] = $selectedSourceKeys
$pipelineSummary['per_source_summaries'] = @([ordered]@{
    source_key='newmark'; verified_listings=4535
    transaction_breakdown=[ordered]@{sale=3000;lease=2000;both=465;measurement='current_staged_inventory_transaction_types'}
})
$pipelineSummary['failed_source_keys'] = @('cbre')
$pipelineSummary['pending_source_keys'] = @('jll')
$pipelineSummary['complete'] = $false
Save-Summary $pipelineSummary
$partialRecord = New-Record
$partialRecord.nativeExitCode = 1
$partial = Get-CreVerifiedSummary $partialRecord 'pipeline-partial'
Assert-Cre ($null -ne $partial -and -not $partial.complete) 'A qualified partial pipeline retains exact source summaries after a nonzero exit.'
$pipelineArgv = [IO.File]::ReadAllLines($argvFile)
Assert-Cre ($pipelineArgv -contains '--pipeline-source-keys' -and $pipelineArgv -contains 'newmark,cbre,jll' -and $pipelineArgv -notcontains '--source') 'Pipeline helper receives the exact finite source selection.'
$partialText = Get-CreNotificationText $partialRecord 'Terminal' 1 $partial
Assert-Cre ($partialText.StartsWith("$warningMark *CRE pipeline | Needs attention*`n`n") -and $partialText.Contains("`n    *For sale:* 3,000 | *For lease:* 2,000 | *Both:* 465") -and $partialText.Contains('Both counts overlap and are included in the For sale and For lease figures.') -and $partialText.Contains("`n`n$bullet *Failed:* 1 | *Pending:* 1") -and $partialText.Contains('Partial result') -and $partialText -notmatch 'unique properties') 'Partial terminal text has a distinct status, spaced source breakdown, no cross-source total and clear failure state.'
$pipelineSummary['per_source_summaries'][0].transaction_breakdown.both = 5000
Save-Summary $pipelineSummary
$invalidPipelineBreakdown = Get-CreVerifiedSummary $partialRecord 'pipeline-invalid-breakdown'
$invalidPipelineText = Get-CreNotificationText $partialRecord 'Terminal' 1 $invalidPipelineBreakdown
Assert-Cre ($null -ne $invalidPipelineBreakdown -and $invalidPipelineBreakdown.per_source_summaries[0].verified_listings -eq 4535 -and $null -eq $invalidPipelineBreakdown.per_source_summaries[0].transaction_breakdown -and $invalidPipelineText.Contains("`n    *For sale / lease / both:* unavailable")) 'Invalid optional pipeline counts preserve the verified total and display breakdown as unavailable.'
$pipelineSummary['pending_source_keys'] = @('cbre')
Save-Summary $pipelineSummary
Assert-Cre ($null -eq (Get-CreVerifiedSummary $partialRecord 'pipeline-duplicate')) 'Duplicate or missing partition members cannot enter the formatter.'
$many = @()
foreach ($key in 1..51) {
    $many += [pscustomobject]@{
        source_key='newmark'; verified_listings=4535
        transaction_breakdown=[pscustomobject]@{sale=3000;lease=2000;both=465}
    }
}
$manySummary = [pscustomobject]@{per_source_summaries=$many; failed_source_keys=@(); pending_source_keys=@(); complete=$true}
$manyText = Get-CreNotificationText $partialRecord 'Terminal' 0 $manySummary
Assert-Cre ($manyText.StartsWith("$successMark *CRE pipeline | Completed*`n`n") -and $manyText.Contains("*Both:* 465`n`n$bullet *Newmark:* 4,535") -and $manyText.Contains('31 additional verified brokerages') -and [Text.Encoding]::UTF8.GetByteCount((@{text=$manyText}|ConvertTo-Json -Compress)) -lt 4096) 'Long terminal summaries space broker blocks and retain the unchanged sender payload bound and omitted-member disclosure.'
Remove-Item Env:CRE_TEST_SUMMARY_JSON,Env:CRE_TEST_SUMMARY_ARGV
Write-Output "Readable CRE notification PASS: $script:checks assertions; proof=$ProofDirectory"
