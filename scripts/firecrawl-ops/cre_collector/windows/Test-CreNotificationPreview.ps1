[CmdletBinding()]
param([string]$ProofDirectory)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $ProofDirectory) {
    $ProofDirectory = Join-Path $PSScriptRoot ('../../../../.scratch/notification-preview-' + [Guid]::NewGuid().ToString('N'))
}
[void](New-Item -ItemType Directory -Path $ProofDirectory -Force)
$ProofDirectory = [IO.Path]::GetFullPath($ProofDirectory)
$script:checks = 0
$previewTool = Join-Path $PSScriptRoot 'Send-CreNotificationPreview.ps1'
function Assert-Cre($condition, [string]$message) {
    if (-not $condition) { throw $message }
    $script:checks++
}
function Set-PrivateFile([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding($false)))
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($owner)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sidValue in @($owner.Value, 'S-1-5-18', 'S-1-5-32-544')) {
        $sid = New-Object Security.Principal.SecurityIdentifier($sidValue)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$missingWebhook = Join-Path $env:TEMP ('cre-preview-missing-' + [Guid]::NewGuid().ToString('N'))
$preview = @(& $previewTool -Scenario Completed -WebhookFile $missingWebhook) -join [Environment]::NewLine
Assert-Cre ($preview.Contains('FORMAT TEST | SAMPLE DATA | NO PIPELINE OR DB READBACK')) 'Preview must identify sample content and disclaim pipeline/database work.'
Assert-Cre ($preview.Contains('*Listings verified:* 10 unique properties') -and $preview.Contains('*For sale:* 6 | *For lease:* 5') -and $preview.Contains('*Both:* 1')) 'Default sample must show the actual formatter with valid mixed counts.'
Assert-Cre ($preview.Contains('PREVIEW ONLY') -and -not [IO.File]::Exists($missingWebhook)) 'Default preview must not read or require a webhook file.'
Assert-Cre ($null -eq ('CreWindowsNotification.Sender' -as [type])) 'Default preview must not load or invoke the network sender.'

$scenarios = @(
    @{ Name = 'Started'; Expected = '| Started*'; Forbidden = 'Listings verified' },
    @{ Name = 'NeedsAttention'; Expected = '| Needs attention*'; Forbidden = 'Listings verified' },
    @{ Name = 'Unverified'; Expected = '| Could not be verified*'; Forbidden = 'Listings verified' }
)
foreach ($scenario in $scenarios) {
    $text = @(& $previewTool -Scenario $scenario.Name) -join [Environment]::NewLine
    Assert-Cre ($text.Contains($scenario.Expected) -and -not $text.Contains($scenario.Forbidden)) "$($scenario.Name) scenario must use the corresponding canonical formatter branch."
}
$custom = @(& $previewTool -Scenario Completed -Source 'cushman-wakefield' -Total 4 -ForSale 3 -ForLease 2 -Both 1) -join [Environment]::NewLine
Assert-Cre ($custom.Contains('*Cushman &amp; Wakefield | Completed*') -and $custom.Contains('*Listings verified:* 4 unique properties')) 'Source and sample counts must be adjustable.'

$textFile = Join-Path $ProofDirectory 'custom-message.txt'
[IO.File]::WriteAllText($textFile, "*Custom sample heading*`n• Short body", (New-Object Text.UTF8Encoding($false)))
$customTextPreview = @(& $previewTool -TextFile $textFile) -join [Environment]::NewLine
Assert-Cre ($customTextPreview.Contains('FORMAT TEST | SAMPLE DATA') -and $customTextPreview.Contains('*Custom sample heading*')) 'Optional UTF-8 text file must be previewed with the sample prefix.'
$bomTextFile = Join-Path $ProofDirectory 'custom-message-bom.txt'
$bomEncoding = New-Object Text.UTF8Encoding($true)
[IO.File]::WriteAllBytes($bomTextFile, $bomEncoding.GetPreamble() + (New-Object Text.UTF8Encoding($false)).GetBytes('*BOM sample*'))
$bomPreview = @(& $previewTool -TextFile $bomTextFile) -join [Environment]::NewLine
Assert-Cre ($bomPreview.Contains('*BOM sample*') -and -not $bomPreview.Contains([char]0xFEFF)) 'UTF-8 BOM is stripped from a custom sample file.'
$oversizedTextFile = Join-Path $ProofDirectory 'oversized-message.txt'
[IO.File]::WriteAllText($oversizedTextFile, ('x' * 3501), (New-Object Text.UTF8Encoding($false)))
$oversizedTextRejected = $false
try { & $previewTool -TextFile $oversizedTextFile | Out-Null }
catch { $oversizedTextRejected = $true }
Assert-Cre $oversizedTextRejected 'Oversized custom text must be rejected before preview or send.'
$escapedTextFile = Join-Path $ProofDirectory 'escaped-oversized-message.txt'
[IO.File]::WriteAllText($escapedTextFile, ('"' * 3500), (New-Object Text.UTF8Encoding($false)))
$escapedTextRejected = $false
try { & $previewTool -TextFile $escapedTextFile | Out-Null }
catch { $escapedTextRejected = $true }
Assert-Cre $escapedTextRejected 'Serialized payload must remain within the sender byte limit.'

$invalidCountRejected = $false
try { & $previewTool -Mode Send -Total 10 -ForSale 1 -ForLease 1 -Both 0 -WebhookFile $missingWebhook | Out-Null }
catch { $invalidCountRejected = $true }
Assert-Cre $invalidCountRejected 'Inconsistent sample numbers must be refused before the sender is loaded.'
$missingTextRejected = $false
try { & $previewTool -TextFile (Join-Path $ProofDirectory 'missing.txt') | Out-Null }
catch { $missingTextRejected = $true }
Assert-Cre $missingTextRejected 'A requested custom text file must exist.'

$fakeSource = @'
using System;
using System.IO;
public static class FakeCurl {
    public static int Main() {
        string config = Console.In.ReadToEnd();
        File.WriteAllText(Environment.GetEnvironmentVariable("CRE_PREVIEW_CAPTURE_PATH"), config);
        Console.Write("200");
        return 0;
    }
}
'@
$fakeSourcePath = Join-Path $ProofDirectory 'fake-curl.cs'
$fakeCurl = Join-Path $ProofDirectory 'curl.exe'
$capturePath = Join-Path $ProofDirectory 'fake-curl-input.txt'
[IO.File]::WriteAllText($fakeSourcePath, $fakeSource)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:exe "/out:$fakeCurl" $fakeSourcePath
if ($LASTEXITCODE -ne 0) { throw 'Offline curl fixture compilation failed.' }

$originalPath = $env:PATH
$originalCapture = $env:CRE_PREVIEW_CAPTURE_PATH
$credentialDirectory = Join-Path $env:TEMP ('cre-preview-private-' + [Guid]::NewGuid().ToString('N'))
$credentialPath = Join-Path $credentialDirectory 'webhook.txt'
$unsafeCredentialPath = Join-Path $ProofDirectory 'unsafe-webhook.txt'
[void](New-Item -ItemType Directory -Path $credentialDirectory -Force)
$env:PATH = "$ProofDirectory;$originalPath"
$env:CRE_PREVIEW_CAPTURE_PATH = $capturePath
try {
    $missingSendRejected = $false
    try { & $previewTool -Mode Send -WebhookFile $missingWebhook | Out-Null }
    catch { $missingSendRejected = $true }
    Assert-Cre ($missingSendRejected -and -not [IO.File]::Exists($capturePath)) 'Missing webhook must be refused without launching transport.'

    Set-PrivateFile $unsafeCredentialPath 'https://sample.invalid/hook'
    $unsafeSendRejected = $false
    try { & $previewTool -Mode Send -WebhookFile $unsafeCredentialPath | Out-Null }
    catch { $unsafeSendRejected = $true }
    Assert-Cre ($unsafeSendRejected -and -not [IO.File]::Exists($capturePath)) 'Repository-local webhook must be rejected without launching transport.'

    Set-PrivateFile $credentialPath 'https://sample.invalid/hook'
    $sendTextFile = Join-Path $ProofDirectory 'custom-sent-message.txt'
    [IO.File]::WriteAllText($sendTextFile, "*Custom send sample*`n• No pipeline ran", (New-Object Text.UTF8Encoding($false)))
    $sendOutput = @(& $previewTool -Mode Send -TextFile $sendTextFile -WebhookFile $credentialPath) -join [Environment]::NewLine
    $capturedConfig = [IO.File]::ReadAllText($capturePath)
    Assert-Cre ($sendOutput -match 'Status\s*:\s*http_accepted' -and $sendOutput.Contains('verify the exact labeled sample')) 'Explicit send must report transport acceptance without claiming delivery proof.'
    Assert-Cre ($capturedConfig.Contains('FORMAT TEST | SAMPLE DATA') -and $capturedConfig.Contains('*Custom send sample*') -and $capturedConfig.Contains('https://sample.invalid/hook')) 'Explicit send must use the existing sender and include the labeled custom sample payload.'
} finally {
    $env:PATH = $originalPath
    if ($null -eq $originalCapture) { Remove-Item Env:CRE_PREVIEW_CAPTURE_PATH -ErrorAction SilentlyContinue }
    else { $env:CRE_PREVIEW_CAPTURE_PATH = $originalCapture }
    Remove-Item -LiteralPath $credentialDirectory -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $unsafeCredentialPath -Force -ErrorAction SilentlyContinue
}

Write-Output "CRE notification preview PASS: $script:checks assertions; proof=$ProofDirectory"
