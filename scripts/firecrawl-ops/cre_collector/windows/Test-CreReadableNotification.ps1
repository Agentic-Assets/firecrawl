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
foreach ($name in @('Get-CreBrokerageName','Test-CreDaumSubsetSummary','Test-CreJllSubsetSummary','Test-CreSourceSubsetSummary','Test-CreMatthewsSubsetSummary','Get-CreMatthewsWarningLines','Get-CreVerifiedSummary','Get-CreNotificationText','Set-CreNotification')) {
    $definition=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true))
    Assert-Cre ($definition.Count -eq 1) "Exactly one shipped $name function is required."
    if ($name -eq 'Set-CreNotification') {
        # A real file preserves PSScriptRoot for the unchanged shipped function.
        $senderFile=Join-Path $ProofDirectory 'Set-CreNotification.ps1'
        [IO.File]::WriteAllText($senderFile,$definition[0].Extent.Text)
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'CreWindowsNotification.cs') -Destination (Join-Path $ProofDirectory 'CreWindowsNotification.cs')
        . $senderFile
    } else {
        . ([scriptblock]::Create($definition[0].Extent.Text))
    }
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
function Get-Command { param($Name,$CommandType,$ErrorAction) if ($Name -notin @('wsl.exe','curl.exe')) { throw 'Unexpected executable lookup.' }; [pscustomobject]@{Source=$fixtureExe} }
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
Assert-Cre ($start.StartsWith("`n$rocket *Newmark | Started*`n`n") -and $start.Contains('collection has not started')) 'Start must have spacing before and after its status heading and avoid claiming collection has happened.'
Assert-Cre ($finish.StartsWith("`n$successMark *Newmark | Completed*`n`n") -and $finish.Contains('*Listings verified:* 4,535 unique properties')) 'Terminal message must have spacing before and after its status heading and label the verified generation total.'
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
Assert-Cre ($failure.StartsWith("`n$warningMark *Newmark | Needs attention*`n`n") -and $failure -notmatch 'Completed|Listings verified') 'A nonzero result must not be promoted by available metrics and must be spaced from adjacent alerts.'
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
$Source = 'savills'
$AdmitBaselineHoldAdditively = $true
Save-Summary (New-Summary)
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'savills-unbound')) 'Additive hold counts require an explicit non-coverage summary.'
$heldSummary = New-Summary
$heldSummary['baseline_hold_retained'] = $true
$heldSummary['whole_source_coverage'] = $false
Save-Summary $heldSummary
$held = Get-CreVerifiedSummary (New-Record) 'savills-held'
Assert-Cre ($null -ne $held) 'A bound additive refresh can retain verified row counts.'
$heldText = Get-CreNotificationText (New-Record) 'Terminal' 0 $held
Assert-Cre ($heldText.Contains('historical baseline hold retained; additive refresh only.')) 'Completion must disclose the unresolved coverage hold.'
$heldSummary['whole_source_coverage'] = $true
Save-Summary $heldSummary
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'savills-overclaim')) 'Additive carrier cannot certify whole-source coverage.'
$Source = 'newmark'
$AdmitBaselineHoldAdditively = $false
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
Assert-Cre ($partialText.StartsWith("`n$warningMark *CRE pipeline | Needs attention*`n`n") -and $partialText.Contains("`n    *For sale:* 3,000 | *For lease:* 2,000 | *Both:* 465") -and $partialText.Contains('Both counts overlap and are included in the For sale and For lease figures.') -and $partialText.Contains("`n`n$bullet *Failed:* 1 | *Pending:* 1") -and $partialText.Contains('Partial result') -and $partialText -notmatch 'unique properties') 'Partial terminal text has a distinct status, spaced source breakdown, no cross-source total and clear failure state.'
$pipelineSummary['per_source_summaries'][0].transaction_breakdown.both = 5000
Save-Summary $pipelineSummary
$invalidPipelineBreakdown = Get-CreVerifiedSummary $partialRecord 'pipeline-invalid-breakdown'
$invalidPipelineText = Get-CreNotificationText $partialRecord 'Terminal' 1 $invalidPipelineBreakdown
Assert-Cre ($null -ne $invalidPipelineBreakdown -and $invalidPipelineBreakdown.per_source_summaries[0].verified_listings -eq 4535 -and $null -eq $invalidPipelineBreakdown.per_source_summaries[0].transaction_breakdown -and $invalidPipelineText.Contains("`n    *For sale / lease / both:* unavailable")) 'Invalid optional pipeline counts preserve the verified total and display breakdown as unavailable.'
Assert-Cre (-not $invalidPipelineText.Contains('Both counts overlap')) 'Unavailable transaction figures must not gain an overlap explanation.'
$emptyPipelineSummary = New-Summary
$emptyPipelineSummary['selected_source_keys'] = $selectedSourceKeys
$emptyPipelineSummary['per_source_summaries'] = @()
$emptyPipelineSummary['failed_source_keys'] = @('newmark','cbre')
$emptyPipelineSummary['pending_source_keys'] = @('jll')
$emptyPipelineSummary['complete'] = $false
Save-Summary $emptyPipelineSummary
$emptyPipelineRead = Get-CreVerifiedSummary $partialRecord 'pipeline-no-qualified-counts'
$emptyPipelineText = Get-CreNotificationText $partialRecord 'Terminal' 1 $emptyPipelineRead
Assert-Cre ($null -ne $emptyPipelineRead -and $emptyPipelineText.Contains('*Failed:* 2 | *Pending:* 1') -and $emptyPipelineText.Contains('*Newmark:* Failed; verified counts unavailable.') -and $emptyPipelineText.Contains('Partial result') -and $emptyPipelineText -notmatch 'Both counts overlap|\*For sale:|\*For lease:|Completed') 'A failed pipeline without qualified sources preserves failure and pending scope without referring to absent transaction figures.'
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
Assert-Cre ($manyText.StartsWith("`n$successMark *CRE pipeline | Completed*`n`n") -and $manyText.Contains("*Both:* 465`n`n$bullet *Newmark:* 4,535") -and $manyText.Contains('31 additional verified brokerages') -and [Text.Encoding]::UTF8.GetByteCount((@{text=$manyText}|ConvertTo-Json -Compress)) -lt 4096) 'Long terminal summaries space broker blocks and retain the unchanged sender payload bound and omitted-member disclosure.'
$Pipeline = $false
$Source = 'cbre-dealflow'
$selectedSourceArgument = $Source
$indexed = New-Summary
$indexed['verified_inventory_only'] = 538
Save-Summary $indexed
$indexedRead = Get-CreVerifiedSummary (New-Record) 'indexed-serial'
$indexedText = Get-CreNotificationText (New-Record) 'Terminal' 0 $indexedRead
Assert-Cre ($null -ne $indexedRead -and $indexedRead.verified_listings -eq 4535 -and $indexedText.Contains('*Property listings verified:* 4,535') -and $indexedText.Contains('*Additional inventory:* 538 records checked separately; property details not refreshed.') -and $indexedText.Contains('*For sale:* 3,000')) 'Serial inventory entries stay separate from property listings and their transaction counts.'
foreach ($bad in @($true, '538', -1, 0, $null)) {
    $indexed['verified_inventory_only'] = $bad
    Save-Summary $indexed
    Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) ('invalid-index-'+[Guid]::NewGuid().ToString('N')))) 'Malformed optional inventory scope refuses the entire summary.'
}
$Pipeline = $true
$selectedSourceKeys = @('cbre','cbre-dealflow')
$selectedSourceArgument = $selectedSourceKeys -join ','
$actualScope = New-Summary
$actualScope['selected_source_keys'] = $selectedSourceKeys
$actualScope['per_source_summaries'] = @(
    [ordered]@{source_key='cbre';verified_listings=19501;transaction_breakdown=[ordered]@{sale=6199;lease=15022;both=1720;measurement='current_staged_inventory_transaction_types'}},
    [ordered]@{source_key='cbre-dealflow';verified_listings=1390;verified_inventory_only=538;transaction_breakdown=[ordered]@{sale=1379;lease=11;both=0;measurement='current_staged_inventory_transaction_types'}}
)
$actualScope['failed_source_keys'] = @()
$actualScope['pending_source_keys'] = @()
$actualScope['complete'] = $true
Save-Summary $actualScope
$actualRead = Get-CreVerifiedSummary (New-Record) 'indexed-pipeline'
$actualText = Get-CreNotificationText (New-Record) 'Terminal' 0 $actualRead
Assert-Cre ($null -ne $actualRead -and $actualText.Contains('19,501 listings verified') -and $actualText.Contains('1,390 property listings verified') -and $actualText.Contains('*For sale:* 6,199 | *For lease:* 15,022 | *Both:* 1,720') -and $actualText.Contains('*For sale:* 1,379 | *For lease:* 11 | *Both:* 0') -and $actualText.Contains('*Additional inventory:* 538 records checked separately; property details not refreshed.') -and $actualText -notmatch '1,928 canonical|20,891') 'Mixed pipeline displays exact canonical transaction counts separately from index cards without a cross-source total.'
$actualScope['per_source_summaries'][1].verified_inventory_only = $true
Save-Summary $actualScope
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'invalid-pipeline-index')) 'Malformed pipeline inventory count cannot become verified scope.'
# DAUM subset is independently scoped, including a fully resolved subset run.
$Pipeline = $false
$Source = 'daum-commercial'; $selectedSourceArgument = $Source
$AdmitDaumVerifiedSubset = $true
function New-DaumSubsetSummary {
    $value = New-Summary
    $value['measurement'] = 'generation_exact_verified_subset'
    $value['qualification'] = 'qualified_partial_subset'
    $value['partial'] = $true
    $value['whole_source_coverage'] = $false
    $value['inventory_count'] = 562
    $value['emitted_count'] = 560
    $value['held_count'] = 2
    $value['whole_lease_coverage'] = 'unknown'
    $value['verified_listings'] = 560
    $value['transaction_breakdown'] = [ordered]@{sale=374;lease=372;both=186;measurement='current_staged_inventory_transaction_types'}
    return $value
}
Save-Summary (New-DaumSubsetSummary)
$subset = Get-CreVerifiedSummary (New-Record) 'daum-subset'
Assert-Cre ($null -ne $subset -and $subset.emitted_count -eq 560) 'Explicit DAUM subset accepts exact qualified emitted readback.'
$subsetText = Get-CreNotificationText (New-Record) 'Terminal' 0 $subset
Assert-Cre ($subsetText.Contains('Verified subset refreshed') -and $subsetText.Contains('560') -and $subsetText.Contains('2 held') -and $subsetText.Contains('whole brokerage incomplete') -and $subsetText.Contains('374') -and $subsetText.Contains('372') -and $subsetText -notmatch 'Completed|all listings|whole-source complete') 'Subset text discloses emitted/held/transaction scope without complete-source success wording.'
foreach ($mutation in @('whole','partial','count','inventory','held','coverage','qualification','breakdown','generation','quality','source')) {
    $value = New-DaumSubsetSummary
    switch ($mutation) {
        'whole' { $value.whole_source_coverage = $true }
        'partial' { $value.partial = $false }
        'count' { $value.emitted_count = '560' }
        'inventory' { $value.inventory_count = 563 }
        'held' { $value.held_count = -1 }
        'coverage' { $value.whole_lease_coverage = 'proved' }
        'qualification' { $value.qualification = 'complete' }
        'breakdown' { $value.transaction_breakdown.both = 0 }
        'generation' { $value.run_id = 'other-generation' }
        'quality' { $value.quality_no_regression = $false }
        'source' { $value.source = 'newmark' }
    }
    Save-Summary $value
    Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) ('subset-invalid-'+$mutation))) 'Malformed subset cannot be relabeled complete or enter verified formatter.'
}
$emptyLease = New-DaumSubsetSummary
$emptyLease.transaction_breakdown = [ordered]@{sale=560;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}
Save-Summary $emptyLease
$emptyRead = Get-CreVerifiedSummary (New-Record) 'subset-zero-lease'
$emptyText = Get-CreNotificationText (New-Record) 'Terminal' 0 $emptyRead
Assert-Cre ($null -ne $emptyRead -and $emptyText.Contains('0 verified subset leases') -and $emptyText.Contains('whole lease coverage unknown')) 'Zero emitted lease with holds never establishes known-empty whole lease inventory.'
$zeroHold = New-DaumSubsetSummary
$zeroHold.held_count = 0; $zeroHold.inventory_count = 560; $zeroHold.whole_lease_coverage = 'proved'
Save-Summary $zeroHold
$zeroRead = Get-CreVerifiedSummary (New-Record) 'subset-zero-hold'
Assert-Cre ($null -ne $zeroRead -and (Get-CreNotificationText (New-Record) 'Terminal' 0 $zeroRead).Contains('whole brokerage incomplete')) 'No holds still retains explicit partial scope.'
$unknown = New-DaumSubsetSummary
$unknown.held_count = 0; $unknown.inventory_count = 560; $unknown.whole_lease_coverage = 'unknown'
$unknown['retained_unknown_tenure_count'] = 5
$unknown.transaction_breakdown.sale = 369
Save-Summary $unknown
$unknownRead = Get-CreVerifiedSummary (New-Record) 'subset-unknown'
$unknownText = Get-CreNotificationText (New-Record) 'Terminal' 0 $unknownRead
Assert-Cre ($null -ne $unknownRead -and $unknownText.Contains('Unknown tenure:* 5') -and $unknownText.Contains('whole lease coverage unknown')) 'Retained unknowns are distinct and keep lease coverage unknown without holds.'
$unknown.whole_lease_coverage = 'proved'
Save-Summary $unknown
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'subset-unknown-false-coverage')) 'Unknown tenure cannot claim proved lease coverage.'
$AdmitDaumVerifiedSubset = $false
Save-Summary (New-DaumSubsetSummary)
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'subset-unbound')) 'Subset summary cannot borrow complete admission.'
$AdmitDaumVerifiedSubset = $true
Save-Summary (New-Summary)
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'complete-subset-flag')) 'Explicit subset mode cannot accept complete receipt in its place.'

$Pipeline = $true
$selectedSourceKeys = @('daum-commercial','newmark'); $selectedSourceArgument = $selectedSourceKeys -join ','
$subsetRow = New-DaumSubsetSummary
$subsetRow['source_key'] = 'daum-commercial'
$mixedSubset = New-Summary
$mixedSubset['selected_source_keys'] = $selectedSourceKeys
$mixedSubset['per_source_summaries'] = @($subsetRow, [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$mixedSubset['qualified_subset_source_keys'] = @('daum-commercial')
$mixedSubset['failed_source_keys'] = @(); $mixedSubset['pending_source_keys'] = @(); $mixedSubset['complete'] = $false
Save-Summary $mixedSubset
$mixedRead = Get-CreVerifiedSummary (New-Record) 'subset-mixed-pipeline'
$mixedText = Get-CreNotificationText (New-Record) 'Terminal' 0 $mixedRead
Assert-Cre ($null -ne $mixedRead -and $mixedText.Contains('*Complete brokerages:* 1 | *Qualified subsets:* 1') -and $mixedText.Contains('*Both:* 186') -and $mixedText.Contains('whole brokerage incomplete') -and $mixedText -notmatch 'Completed|did not finish successfully') 'Mixed pipeline counts qualified subsets separately from complete brokerages.'
$unknownRow = New-DaumSubsetSummary
$unknownRow['source_key'] = 'daum-commercial'; $unknownRow.held_count = 0; $unknownRow.inventory_count = 560
$unknownRow['retained_unknown_tenure_count'] = 5; $unknownRow.transaction_breakdown.sale = 369
$mixedSubset.per_source_summaries = @($unknownRow, [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
Save-Summary $mixedSubset
$unknownMixedRead = Get-CreVerifiedSummary (New-Record) 'subset-unknown-mixed'
$unknownMixedText = Get-CreNotificationText (New-Record) 'Terminal' 0 $unknownMixedRead
Assert-Cre ($null -ne $unknownMixedRead -and $unknownMixedText.Contains('*Unknown tenure:* 5') -and $unknownMixedText.Contains('whole lease coverage unknown')) 'Mixed pipeline retains the unknown partition and truthful lease scope.'
$mixedSubset.per_source_summaries = @($subsetRow, [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
foreach ($bad in @('complete','partition','duplicate','string-partition','low-floor','no-opt-in')) {
    $value = $mixedSubset | ConvertTo-Json -Depth 10 | ConvertFrom-Json
    switch ($bad) {
        'complete' { $value.complete = $true }
        'partition' { $value.qualified_subset_source_keys = @() }
        'duplicate' { $value.qualified_subset_source_keys = @('daum-commercial','daum-commercial') }
        'string-partition' { $value.qualified_subset_source_keys = 'daum-commercial' }
        'low-floor' {
            $value.per_source_summaries[0].verified_listings = 99; $value.per_source_summaries[0].emitted_count = 99
            $value.per_source_summaries[0].inventory_count = 101
            $value.per_source_summaries[0].transaction_breakdown.sale = 99; $value.per_source_summaries[0].transaction_breakdown.lease = 99; $value.per_source_summaries[0].transaction_breakdown.both = 99
        }
        'no-opt-in' { $AdmitDaumVerifiedSubset = $false }
    }
    Save-Summary $value
    Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) ('subset-invalid-pipeline-'+$bad))) 'Malformed or subfloor pipeline subset never counts as full source acceptance.'
    $AdmitDaumVerifiedSubset = $true
}

# Pyramid uses its existing current-inventory admission for an automatic partial.
$AdmitDaumVerifiedSubset = $false
$selectedAdmissionKeys = @('pyramid-brokerage')
$Source = 'pyramid-brokerage'; $selectedSourceArgument = $Source; $Pipeline = $false
$pyramid = New-DaumSubsetSummary
$pyramid.source = $Source
$pyramid.inventory_count = 102; $pyramid.emitted_count = 99; $pyramid.held_count = 3; $pyramid.verified_listings = 99
$pyramid.transaction_breakdown = [ordered]@{sale=99;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}
Save-Summary $pyramid
$pyramidRead = Get-CreVerifiedSummary (New-Record) 'pyramid-subset'
$pyramidText = Get-CreNotificationText (New-Record) 'Terminal' 0 $pyramidRead
Assert-Cre ($null -ne $pyramidRead -and $pyramidText.Contains('3 held') -and $pyramidText.Contains('whole brokerage incomplete')) 'Admitted Pyramid partial reports held records without claiming completion.'
$selectedAdmissionKeys = @()
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'pyramid-no-admission')) 'Pyramid partial requires its existing admission.'
$selectedAdmissionKeys = @('pyramid-brokerage'); $Pipeline = $true
$selectedSourceKeys = @('pyramid-brokerage','newmark'); $selectedSourceArgument = $selectedSourceKeys -join ','
$pyramidRow = New-DaumSubsetSummary; $pyramidRow.source_key = 'pyramid-brokerage'
$pyramidRow.inventory_count = 102; $pyramidRow.emitted_count = 99; $pyramidRow.held_count = 3; $pyramidRow.verified_listings = 99
$pyramidRow.transaction_breakdown = [ordered]@{sale=99;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}
$mixedPyramid = New-Summary
$mixedPyramid['selected_source_keys'] = $selectedSourceKeys
$mixedPyramid['per_source_summaries'] = @($pyramidRow, [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$mixedPyramid['qualified_subset_source_keys'] = @('pyramid-brokerage')
$mixedPyramid['failed_source_keys'] = @(); $mixedPyramid['pending_source_keys'] = @(); $mixedPyramid['complete'] = $false
Save-Summary $mixedPyramid
$mixedPyramidRead = Get-CreVerifiedSummary (New-Record) 'pyramid-mixed'
Assert-Cre ($null -ne $mixedPyramidRead -and (Get-CreNotificationText (New-Record) 'Terminal' 0 $mixedPyramidRead).Contains('*Complete brokerages:* 1 | *Qualified subsets:* 1')) 'Mixed Pyramid partial keeps healthy sibling complete and reports partial separately.'
$selectedAdmissionKeys = @()
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'pyramid-mixed-no-admission')) 'Mixed Pyramid partial requires existing admission.'
$Source = 'jll-investor'; $selectedSourceArgument = $Source; $Pipeline = $false
$AdmitDaumVerifiedSubset = $false; $selectedAdmissionKeys = @()
$jll = New-DaumSubsetSummary
$jll.Remove('inventory_count')
$jll.source = 'jll-investor'; $jll.emitted_count = 3; $jll.verified_listings = 3; $jll.held_count = 1
$jll.whole_lease_coverage = 'unknown'; $jll['excluded_source_identities'] = 'unknown'
$jll.transaction_breakdown = [ordered]@{sale=3;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}
Save-Summary $jll
$jllRead = Get-CreVerifiedSummary (New-Record) 'jll-partial'
$jllText = Get-CreNotificationText (New-Record) 'Terminal' 0 $jllRead
Assert-Cre ($null -ne $jllRead -and $jllText.Contains('1 database identity conflicts held') -and $jllText.Contains('source exclusions unknown')) 'JLL partial distinguishes database holds and unknown source exclusions.'
$jll.excluded_source_identities = 'none'; Save-Summary $jll
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'jll-invalid')) 'JLL source exclusion scope must stay unknown.'
$Pipeline = $true; $selectedSourceKeys = @('jll-investor','newmark'); $selectedSourceArgument = $selectedSourceKeys -join ','
$jll.excluded_source_identities = 'unknown'; $jll['source_key'] = 'jll-investor'
$mixedJll = New-Summary
$mixedJll['selected_source_keys'] = $selectedSourceKeys
$mixedJll['per_source_summaries'] = @($jll, [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$mixedJll['qualified_subset_source_keys'] = @('jll-investor')
$mixedJll['failed_source_keys'] = @(); $mixedJll['pending_source_keys'] = @(); $mixedJll['complete'] = $false
Save-Summary $mixedJll
$jllMixedRead = Get-CreVerifiedSummary (New-Record) 'jll-mixed'
Assert-Cre ($null -ne $jllMixedRead -and (Get-CreNotificationText (New-Record) 'Terminal' 0 $jllMixedRead).Contains('source exclusions unknown')) 'JLL partial and healthy sibling remain separately reported.'
$mixedJll['per_source_summaries'] = @([ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$mixedJll['qualified_subset_source_keys'] = @()
$mixedJll['failed_source_keys'] = @('jll-investor')
$mixedJll['unverifiable_source_keys'] = @('jll-investor')
Save-Summary $mixedJll
$unverifiableRead = Get-CreVerifiedSummary (New-Record) 'jll-unverifiable'
$unverifiableText = Get-CreNotificationText (New-Record) 'Terminal' 0 $unverifiableRead
Assert-Cre ($null -ne $unverifiableRead -and $unverifiableText.Contains('Readback could not be verified') -and $unverifiableText.Contains('100 listings verified by database readback') -and $unverifiableText.Contains('*Failed:* 0 | *Unverifiable:* 1')) 'Unverifiable JLL preserves peer counts without claiming failed execution.'
$mixedJll['unverifiable_source_keys'] = @('newmark'); Save-Summary $mixedJll
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'jll-unverifiable-forged')) 'Unverifiable source must be the failed JLL source.'
$Source = 'matthews'; $selectedSourceArgument = $Source; $Pipeline = $false
$AdmitDaumVerifiedSubset = $false; $selectedAdmissionKeys = @()
$matthews = New-Summary
$matthews.Remove('transaction_breakdown')
$matthews.source = 'matthews'; $matthews.verified_listings = 1
$matthews.measurement = 'existing_parents_fields_filled'
$matthews['qualification'] = 'qualified_partial_subset'; $matthews['partial'] = $true
$matthews['whole_source_coverage'] = $false; $matthews['candidate_count'] = 3
$matthews['emitted_count'] = 1; $matthews['held_count'] = 1; $matthews['source_held_count'] = 2
$matthews['receipt_warnings'] = @([ordered]@{transaction='sale';reason='held_receipt_counts_unverified';reported=@(0,0,0);verified=@(1,0,0)})
Save-Summary $matthews
$matthewsRead = Get-CreVerifiedSummary (New-Record) 'matthews-serial'
$matthewsText = Get-CreNotificationText (New-Record) 'Terminal' 0 $matthewsRead
Assert-Cre ($null -ne $matthewsRead -and $matthewsText.Contains('1 existing parents') -and
    $matthewsText.Contains('reported 0/0/0; verified 1/0/0') -and
    $matthewsText.Contains('12 fields on active existing parents') -and $matthewsText.Contains('New IDs, pricing, new children') -and
    $matthewsText.Contains('whole brokerage incomplete')) 'Serial Matthews renders verified fields, limitations and receipt discrepancy.'
$invalidMatthews = $matthews | ConvertTo-Json -Depth 10 | ConvertFrom-Json
$invalidMatthews.receipt_warnings[0].verified = @(0,0,0)
Save-Summary $invalidMatthews
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'matthews-invalid-warning')) 'Invalid Matthews warning cannot be presented as verified.'
$matthews.verified_listings = 0; $matthews.emitted_count = 0; $matthews.receipt_warnings = @()
Save-Summary $matthews
$zeroMatthews = Get-CreVerifiedSummary (New-Record) 'matthews-zero'
Assert-Cre ($null -ne $zeroMatthews -and (Get-CreNotificationText (New-Record) 'Terminal' 0 $zeroMatthews).Contains('0 existing parents')) 'A verified partial with no changed parents remains honest and visible.'
$matthews.verified_listings = 1; $matthews.emitted_count = 1
$matthews.receipt_warnings = @([ordered]@{transaction='sale';reason='held_receipt_counts_unverified';reported=@(0,0,0);verified=@(1,0,0)})
$Pipeline = $true; $selectedSourceKeys = @('matthews','newmark'); $selectedSourceArgument = $selectedSourceKeys -join ','
$matthews['source_key'] = 'matthews'
$mixedMatthews = New-Summary
$mixedMatthews['selected_source_keys'] = $selectedSourceKeys
$mixedMatthews['per_source_summaries'] = @($matthews, [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$mixedMatthews['qualified_subset_source_keys'] = @('matthews')
$mixedMatthews['failed_source_keys'] = @(); $mixedMatthews['pending_source_keys'] = @(); $mixedMatthews['complete'] = $false
Save-Summary $mixedMatthews
$matthewsMixedRead = Get-CreVerifiedSummary (New-Record) 'matthews-mixed'
$matthewsMixedText = Get-CreNotificationText (New-Record) 'Terminal' 0 $matthewsMixedRead
Assert-Cre ($null -ne $matthewsMixedRead -and $matthewsMixedText.Contains('*Matthews:* 1 existing parents') -and
    $matthewsMixedText.Contains('reported 0/0/0; verified 1/0/0') -and $matthewsMixedText.Contains('*Newmark:* 100 listings verified') -and
    $matthewsMixedText.Contains('Whole brokerage incomplete')) "Pipeline Matthews warning and healthy sibling remain separately visible. Reader null: $($null -eq $matthewsMixedRead); text: $matthewsMixedText"

$Pipeline = $true; $selectedSourceKeys = @('colliers','lyon-stahl','franklin-street','newmark','savills')
$selectedSourceArgument = $selectedSourceKeys -join ','
$mixedSources = New-Summary
$mixedSources['selected_source_keys'] = $selectedSourceKeys
$mixedSources['per_source_summaries'] = @(
    [ordered]@{source_key='colliers';verified_listings=7;emitted_count=7;held_count=2;terminal_stop='worker_failed';measurement='generation_exact_verified_subset';qualification='qualified_partial_subset';partial=$true;whole_source_coverage=$false;transaction_breakdown=[ordered]@{sale=7;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}},
    [ordered]@{source_key='lyon-stahl';verified_listings=3;emitted_count=3;held_count=4;measurement='generation_exact_verified_subset';qualification='qualified_partial_subset';partial=$true;whole_source_coverage=$false},
    [ordered]@{source_key='franklin-street';verified_listings=0;emitted_count=0;held_count=1;measurement='generation_exact_verified_subset';qualification='qualified_partial_subset';partial=$true;whole_source_coverage=$false;buildout_pending=5;held_observations=2;identity_uncertainty=[ordered]@{siteToBuildout='unresolved';possibleDuplicateProperties=$true;reusedSiteIds=@()}},
    [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$mixedSources['qualified_subset_source_keys'] = @('colliers','lyon-stahl','franklin-street')
$mixedSources['failed_source_keys'] = @('savills'); $mixedSources['pending_source_keys'] = @(); $mixedSources['complete'] = $false
Save-Summary $mixedSources
$sourceRead = Get-CreVerifiedSummary (New-Record) 'mixed-source-partials'
Assert-Cre ($null -ne $sourceRead) 'Source-bound partials, complete peer and failed peer remain separately qualified.'
$sourceText = Get-CreNotificationText (New-Record) 'Terminal' 1 $sourceRead
Assert-Cre ($sourceText.Contains('*Colliers:* Verified subset refreshed; 7 emitted; 2 held') -and
    $sourceText.Contains('A later partial worker failed; earlier verified windows were retained.') -and
    $sourceText.Contains('*For sale:* 7 | *For lease:* 0 | *Both:* 0') -and
    $sourceText.Contains('*Lyon-Stahl:* Verified subset refreshed; 3 emitted; 4 held') -and
    $sourceText.Contains('*Franklin Street:* Verified subset refreshed; 0 emitted; 1 held') -and
    $sourceText.Contains('*Buildout pending:* 5 | *Source observations held:* 2') -and $sourceText.Contains('properties may overlap') -and
    $sourceText.Contains('*For sale / lease / both:* unavailable') -and
    $sourceText.Contains('*Newmark:* 100 listings verified') -and $sourceText.Contains('*Savills:* Failed')) 'Partial counts and known zero remain distinct from unavailable transaction counts.'
$stoppedSource = $mixedSources | ConvertTo-Json -Depth 10 | ConvertFrom-Json
$stoppedSource.per_source_summaries[0].terminal_stop = 'source_stopped'
Save-Summary $stoppedSource
$stoppedText = Get-CreNotificationText (New-Record) 'Terminal' 1 (Get-CreVerifiedSummary (New-Record) 'source-stopped')
Assert-Cre ($stoppedText.Contains('Source detail collection stopped; earlier verified windows were retained.')) 'A settled source stop is reported alongside verified windows.'
foreach ($case in @(
    @{name='text'; value='bogus'}, @{name='number'; value=0},
    @{name='boolean'; value=$false}, @{name='array'; value=@('worker_failed')},
    @{name='null'; value=$null}
)) {
    $badSummary = $mixedSources | ConvertTo-Json -Depth 10 | ConvertFrom-Json
    $badSummary.per_source_summaries[0].terminal_stop = $case.value
    Save-Summary $badSummary
    Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) ('invalid-colliers-stop-' + $case.name))) 'Invalid terminal stop must be refused.'
}
$foreignStop = $mixedSources | ConvertTo-Json -Depth 10 | ConvertFrom-Json
$foreignStop.per_source_summaries[1] | Add-Member -NotePropertyName terminal_stop -NotePropertyValue 'worker_failed'
Save-Summary $foreignStop
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'foreign-stop')) 'Terminal stop on a foreign source must be refused.'
$invalidBreakdown = $mixedSources | ConvertTo-Json -Depth 10 | ConvertFrom-Json
$invalidBreakdown.per_source_summaries[0].transaction_breakdown.sale = 6
Save-Summary $invalidBreakdown
$invalidBreakdownRead = Get-CreVerifiedSummary (New-Record) 'mixed-source-invalid-breakdown'
$invalidBreakdownText = Get-CreNotificationText (New-Record) 'Terminal' 1 $invalidBreakdownRead
Assert-Cre ($null -ne $invalidBreakdownRead -and $null -eq $invalidBreakdownRead.per_source_summaries[0].transaction_breakdown -and $invalidBreakdownText.Contains('*Colliers:* Verified subset refreshed; 7 emitted') -and
    -not $invalidBreakdownText.Contains('*For sale:* 6 | *For lease:* 0 | *Both:* 0')) 'An invalid transaction breakdown does not invent or discard the independently verified total.'
foreach ($mutation in @('coverage','partial','emitted','fractional','held','qualification','franklin-pending','franklin-identity','foreign','declared','duplicate')) {
    $value = $mixedSources | ConvertTo-Json -Depth 10 | ConvertFrom-Json
    switch ($mutation) {
        'coverage' { $value.per_source_summaries[1].whole_source_coverage = $true }
        'partial' { $value.per_source_summaries[1].partial = $false }
        'emitted' { $value.per_source_summaries[1].emitted_count = 4 }
        'fractional' { $value.per_source_summaries[1].emitted_count = 3.5 }
        'held' { $value.per_source_summaries[1].held_count = -1 }
        'qualification' { $value.per_source_summaries[1].qualification = 'complete' }
        'franklin-pending' { $value.per_source_summaries[2].buildout_pending = -1 }
        'franklin-identity' { $value.per_source_summaries[2].identity_uncertainty.siteToBuildout = 'resolved' }
        'foreign' { $value.per_source_summaries[1].source_key = 'savills' }
        'declared' { $value.qualified_subset_source_keys = @('colliers','franklin-street') }
        'duplicate' { $value.failed_source_keys = @('savills','savills') }
    }
    Save-Summary $value
    Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) ('mixed-source-bad-' + $mutation))) "Malformed partial $mutation must be refused."
}
$mixedSources['per_source_summaries'] = @($mixedSources.per_source_summaries[3])
$mixedSources['qualified_subset_source_keys'] = @()
$mixedSources['failed_source_keys'] = @('colliers','lyon-stahl','franklin-street','savills')
$mixedSources['unverifiable_source_keys'] = @('colliers','lyon-stahl','franklin-street')
Save-Summary $mixedSources
$unverifiableSources = Get-CreVerifiedSummary (New-Record) 'mixed-source-unverifiable'
Assert-Cre ($null -ne $unverifiableSources -and (Get-CreNotificationText (New-Record) 'Terminal' 1 $unverifiableSources).Contains('*Unverifiable:* 3')) 'Unverifiable source summaries retain a complete peer without promoting counts.'
$selectedSourceKeys = @('nai-global','newmark','savills')
$selectedSourceArgument = $selectedSourceKeys -join ','
$naiSummary = New-Summary
$naiSummary['selected_source_keys'] = $selectedSourceKeys
$naiSummary['per_source_summaries'] = @(
    [ordered]@{source_key='nai-global';verified_listings=108;emitted_count=108;held_count=6;measurement='generation_exact_verified_subset';qualification='qualified_partial_subset';partial=$true;whole_source_coverage=$false},
    [ordered]@{source_key='newmark';verified_listings=100;transaction_breakdown=[ordered]@{sale=100;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$naiSummary['qualified_subset_source_keys'] = @('nai-global')
$naiSummary['failed_source_keys'] = @('savills'); $naiSummary['pending_source_keys'] = @(); $naiSummary['complete'] = $false
Save-Summary $naiSummary
$naiRead = Get-CreVerifiedSummary (New-Record) 'nai-partial'
$naiText = Get-CreNotificationText (New-Record) 'Terminal' 1 $naiRead
Assert-Cre ($null -ne $naiRead -and $naiText.Contains('*NAI Global:* Verified subset refreshed; 108 emitted; 6 held') -and
    $naiText.Contains('unknown status are excluded') -and $naiText.Contains('*For sale / lease / both:* unavailable') -and
    $naiText.Contains('*Newmark:* 100 listings verified') -and $naiText.Contains('*Savills:* Failed')) 'NAI post subset preserves peer outcomes and does not invent a transaction breakdown.'
$naiInvalid = $naiSummary | ConvertTo-Json -Depth 10 | ConvertFrom-Json
$naiInvalid.per_source_summaries[0].whole_source_coverage = $true
Save-Summary $naiInvalid
Assert-Cre ($null -eq (Get-CreVerifiedSummary (New-Record) 'nai-false-complete')) 'NAI partial must not claim whole-source coverage.'
$Source = 'daum-commercial'; $AdmitDaumVerifiedSubset = $true
$selectedSourceKeys = @('daum-commercial','newmark'); $selectedSourceArgument = $selectedSourceKeys -join ','

# Exercise the actual sender boundary; transport is an in-memory substitute.
$helperHash=(Get-FileHash (Join-Path $PSScriptRoot 'CreWindowsNotification.cs') -Algorithm SHA256).Hash.ToLowerInvariant()
Add-Type -TypeDefinition @"
namespace CreWindowsNotification {
    public static class Identity { public const string Sha256 = "$helperHash"; }
    public static class Sender {
        public static System.Collections.Generic.List<string> Payloads = new System.Collections.Generic.List<string>();
        public static object Send(string credential, string repository, string curl, string payload) {
            Payloads.Add(payload); return new { Status = "synthetic_transport" };
        }
    }
}
"@
$NotificationMode='StartFinish'; $AlertWebhookFile='synthetic-unused'; $AdmitDaumVerifiedSubset=$false
$Pipeline=$true; $selectedSourceKeys=@('newmark','cbre','jll'); $selectedSourceArgument=$selectedSourceKeys -join ','
$senderSummary=New-Summary
$senderSummary['selected_source_keys']=$selectedSourceKeys
$senderSummary['per_source_summaries']=@([ordered]@{source_key='newmark';verified_listings=59;transaction_breakdown=[ordered]@{sale=59;lease=0;both=0;measurement='current_staged_inventory_transaction_types'}})
$senderSummary['failed_source_keys']=@('cbre'); $senderSummary['pending_source_keys']=@('jll'); $senderSummary['complete']=$false
Save-Summary $senderSummary
$record=New-Record; $record.nativeExitCode=1
$script:notificationAttempted=@{Start=$false;Terminal=$false}
Set-CreNotification $record 'actual-sender' 0 'Start'
Set-CreNotification $record 'actual-sender' 1 'Terminal'
Set-CreNotification $record 'actual-sender' 1 'Terminal'
Assert-Cre ([CreWindowsNotification.Sender]::Payloads.Count -eq 2) 'The real sender attempts only one Start and one Finish.'
$payload=[CreWindowsNotification.Sender]::Payloads[1] | ConvertFrom-Json
Assert-Cre ($record.Contains('notificationSummaryCapture') -and $record.notificationSummaryStatus -ceq 'verified') 'Nonzero pipeline terminal must invoke the actual bounded summary reader.'
Assert-Cre ($payload.text.Contains('Needs attention') -and $payload.text.Contains('59 listings verified') -and $payload.text.Contains('*For sale:* 59 | *For lease:* 0') -and $payload.text -notmatch 'Completed') 'Overall failure must retain only qualified brokerage figures.'
Assert-Cre ($payload.text.Contains('*CBRE:* Failed; verified counts unavailable') -and $payload.text.Contains('*JLL:* Pending; verified counts unavailable')) 'Failed and pending brokerages have explicit unavailable counts.'
foreach ($exitCode in @(124, 130)) {
    $record=New-Record; $record.nativeExitCode=$exitCode
    $script:notificationAttempted=@{Start=$false;Terminal=$false}
    Set-CreNotification $record ('interrupted-'+$exitCode) $exitCode 'Terminal'
    $payload=[CreWindowsNotification.Sender]::Payloads[[CreWindowsNotification.Sender]::Payloads.Count-1] | ConvertFrom-Json
    Assert-Cre ($record.notificationSummaryStatus -ceq 'verified' -and $payload.text.Contains('Needs attention') -and $payload.text.Contains('59 listings verified') -and $payload.text.Contains('*CBRE:* Failed; verified counts unavailable') -and $payload.text -notmatch 'Completed') 'Settled deadline/operator summaries retain only verified source counts under overall Needs attention.'
}
foreach ($failure in @('unverified','conflict','audit-failed','nonpipeline')) {
    $record=New-Record; $record.nativeExitCode=1
    switch ($failure) {
        'unverified' { Save-Summary @{schema_version=1;verified=$false} }
        'conflict' { $record.capture.Stdout.RunReferenceConflict=$true }
        'audit-failed' { $record.status='windows_terminal_audit_failed_linux_settlement_unverified' }
        'nonpipeline' { $Pipeline=$false }
    }
    $script:notificationAttempted=@{Start=$false;Terminal=$false}
    Set-CreNotification $record ('sender-refusal-'+$failure) 1 'Terminal'
    $payload=[CreWindowsNotification.Sender]::Payloads[[CreWindowsNotification.Sender]::Payloads.Count-1] | ConvertFrom-Json
    Assert-Cre ($payload.text -notmatch '59 listings|For sale:\* 59|Completed') 'Unavailable or unbound summaries never supply counts.'
    if ($failure -eq 'nonpipeline') { Assert-Cre (-not $record.Contains('notificationSummaryCapture')) 'A nonpipeline failure does not dispatch a summary reader.' }
}

$Tier='enrich'; $Source='newmark'
$record=New-Record; $record.nativeExitCode=20
$text=Get-CreNotificationText $record 'Terminal' 20 $null
Assert-Cre ($text.Contains('Partially completed') -and $text -notmatch 'verified listings') 'Settled partial enrichment must be visible without invented counts.'
$record.nativeExitCode=21
$text=Get-CreNotificationText $record 'Terminal' 21 $null
Assert-Cre ($text.Contains('No items completed') -and $text -notmatch 'verified listings') 'Settled failed enrichment must be visible without invented counts.'
$record.status='windows_terminal_audit_failed_linux_settlement_unverified'
$text=Get-CreNotificationText $record 'Terminal' 20 $null
Assert-Cre ($text.Contains('completion is unverified') -and $text -notmatch 'Partially completed') 'Missing terminal audit must not claim a settled outcome.'
$Tier='monitor'; $Source='svn'
$record=New-Record; $record.nativeExitCode=20
$text=Get-CreNotificationText $record 'Terminal' 20 $null
Assert-Cre ($text.Contains('Partially completed') -and $text.Contains('deferred')) 'Settled partial SVN monitor must report deferral.'
$record.nativeExitCode=22
$text=Get-CreNotificationText $record 'Terminal' 22 $null
Assert-Cre ($text.Contains('Already processed') -and $text.Contains('no new writes')) 'Settled SVN replay must not claim a new completion.'
Remove-Item Env:CRE_TEST_SUMMARY_JSON,Env:CRE_TEST_SUMMARY_ARGV
Write-Output "Readable CRE notification PASS: $script:checks assertions; proof=$ProofDirectory"
