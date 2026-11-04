[CmdletBinding()]
param(
    [ValidateSet('Started', 'Completed', 'VerifiedSubset', 'NeedsAttention', 'Unverified')]
    [string]$Scenario = 'Completed',
    [ValidateSet('Preview', 'Send')]
    [string]$Mode = 'Preview',
    [string]$Source = 'newmark',
    [ValidateRange(0, 500000)]
    [int]$Total = 10,
    [ValidateRange(0, 500000)]
    [int]$ForSale = 6,
    [ValidateRange(0, 500000)]
    [int]$ForLease = 5,
    [ValidateRange(0, 500000)]
    [int]$Both = 1,
    [ValidateRange(0, 500000)]
    [int]$HeldCount = 2,
    [string]$TextFile,
    [string]$WebhookFile = (Join-Path $env:USERPROFILE '.config\cre\cre-pipeline.webhook')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Source -cnotmatch '^[a-z0-9][a-z0-9-]{0,79}$') {
    throw 'Source must be a safe lowercase brokerage key.'
}
if ($Scenario -eq 'VerifiedSubset' -and $Source -cne 'daum-commercial') { throw 'VerifiedSubset sample requires DAUM source.' }
if (-not $TextFile -and ($Both -gt $ForSale -or $Both -gt $ForLease -or ($ForSale + $ForLease - $Both) -ne $Total)) {
    throw 'Sample counts must satisfy sale + lease - both = total, with both no greater than either category.'
}
if ($Mode -eq 'Send' -and [string]::IsNullOrWhiteSpace($WebhookFile)) {
    throw 'Send mode requires a private webhook file path.'
}

if ($TextFile) {
    if (-not (Test-Path -LiteralPath $TextFile -PathType Leaf)) { throw 'The sample text file was not found.' }
    $sampleStream = New-Object IO.FileStream(
        [IO.Path]::GetFullPath($TextFile),
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        if ($sampleStream.Length -gt 3500) { throw 'Sample text file exceeds the 3,500-byte limit.' }
        $textBytes = New-Object byte[] ([int]$sampleStream.Length)
        $offset = 0
        while ($offset -lt $textBytes.Length) {
            $read = $sampleStream.Read($textBytes, $offset, $textBytes.Length - $offset)
            if ($read -le 0) { throw 'Sample text file changed while it was being read.' }
            $offset += $read
        }
    } finally { $sampleStream.Dispose() }
    try { $formatted = (New-Object Text.UTF8Encoding($false, $true)).GetString($textBytes) }
    catch { throw 'Sample text file must be valid UTF-8.' }
    if ($formatted.Length -gt 0 -and $formatted[0] -eq [char]0xFEFF) { $formatted = $formatted.Substring(1) }
    if ([string]::IsNullOrWhiteSpace($formatted)) { throw 'Sample text file cannot be empty.' }
} else {
    $launcherPath = Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
    $launcherText = [IO.File]::ReadAllText($launcherPath)
    $tokens = $null
    $parseErrors = $null
    $launcherAst = [Management.Automation.Language.Parser]::ParseInput($launcherText, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -ne 0) { throw 'The CRE launcher could not be parsed.' }
    foreach ($functionName in @('Get-CreBrokerageName', 'Get-CreNotificationText')) {
        $definitions = @($launcherAst.FindAll({
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $functionName
        }, $true))
        if ($definitions.Count -ne 1) { throw "Expected one canonical $functionName formatter in the CRE launcher." }
        . ([scriptblock]::Create($definitions[0].Extent.Text))
    }

    $script:Source = $Source
    $script:Tier = 'series'
    $record = [ordered]@{
        createdAt = [DateTimeOffset]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        status = 'format_test_sample'
    }
    $phase = 'Terminal'
    $exitCode = 0
    $summary = $null
    switch ($Scenario) {
        'Started' { $phase = 'Start' }
        'Completed' {
            $summary = [pscustomobject]@{
                verified_listings = $Total
                transaction_breakdown = [pscustomobject]@{
                    sale = $ForSale
                    lease = $ForLease
                    both = $Both
                    measurement = 'current_staged_inventory_transaction_types'
                }
            }
        }
        'VerifiedSubset' {
            $summary = [pscustomobject]@{
                verified_listings = $Total; emitted_count = $Total; held_count = $HeldCount
                inventory_count = ($Total + $HeldCount)
                measurement = 'generation_exact_verified_subset'; qualification = 'qualified_partial_subset'
                partial = $true; whole_source_coverage = $false
                whole_lease_coverage = $(if ($HeldCount -gt 0) { 'unknown' } else { 'proved' })
                transaction_breakdown = [pscustomobject]@{sale=$ForSale;lease=$ForLease;both=$Both;measurement='current_staged_inventory_transaction_types'}
            }
        }
        'NeedsAttention' { $exitCode = 23 }
        'Unverified' { $record.status = 'format_test_unverified' }
    }
    $formatted = Get-CreNotificationText $record $phase $exitCode $summary
    if ($Scenario -in @('Completed','VerifiedSubset')) {
        $sampleReadback = "$([char]0x2022) *Database readback:* passed; no quality regression."
        $truthfulReadback = "$([char]0x2022) *Database readback:* sample only; no readback occurred."
        $formatted = $formatted.Replace($sampleReadback, $truthfulReadback)
    }
}
$samplePrefix = [char]::ConvertFromUtf32(0x1F9EA) + ' *FORMAT TEST | SAMPLE DATA | NO PIPELINE OR DB READBACK*'
$message = "$samplePrefix`n$formatted"
$payload = [ordered]@{ text = $message } | ConvertTo-Json -Compress
if ([Text.Encoding]::UTF8.GetByteCount($payload) -gt 4096) { throw 'Formatted sample exceeds the private sender 4,096-byte payload limit.' }
Write-Output $message

if ($Mode -eq 'Preview') {
    Write-Output 'PREVIEW ONLY: no webhook file was read and no network request was made.'
    return
}

$notificationPath = Join-Path $PSScriptRoot 'CreWindowsNotification.cs'
$notificationBytes = [IO.File]::ReadAllBytes($notificationPath)
$sha = [Security.Cryptography.SHA256]::Create()
try { $notificationSha = [BitConverter]::ToString($sha.ComputeHash($notificationBytes)).Replace('-', '').ToLowerInvariant() }
finally { $sha.Dispose() }
if ('CreWindowsNotification.Sender' -as [type]) {
    if (-not ('CreWindowsNotification.Identity' -as [type]) -or [CreWindowsNotification.Identity]::Sha256 -cne $notificationSha) {
        throw 'The loaded private notification sender differs from the checked-in helper.'
    }
} else {
    $notificationSource = (New-Object Text.UTF8Encoding($false, $true)).GetString($notificationBytes)
    Add-Type -TypeDefinition ($notificationSource + "`nnamespace CreWindowsNotification { public static class Identity { public const string Sha256 = `"$notificationSha`"; } }")
}

try {
    $curl = (Microsoft.PowerShell.Core\Get-Command curl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
    $result = [CreWindowsNotification.Sender]::Send($WebhookFile, $repository, $curl, $payload)
    [pscustomobject]@{
        Status = $result.Status
        Reason = $result.Reason
        HttpStatus = $result.HttpStatus
        NativeSettlement = $result.NativeSettlement
    } | Format-List | Out-String | Write-Output
    if ($result.Status -ne 'http_accepted') { throw 'The sample notification transport did not receive a successful HTTP response.' }
    Write-Output 'Transport accepted; verify the exact labeled sample in Slack before claiming delivery.'
} catch {
    throw 'Sample send failed; inspect the safe sender result above and verify the private webhook configuration.'
}
