<# No-network configuration check. Exit0 means the private file is valid,
   never that Slack accepted or delivered a message. Prints no secret/path. #>
[CmdletBinding()]
param(
    [string]$AlertWebhookFile = (Join-Path $env:USERPROFILE '.config/cre/cre-pipeline.webhook'),
    [switch]$Quiet
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$configured = $false
try {
    if ($env:OS -ne 'Windows_NT') { throw 'Windows required.' }
    $helper = Join-Path $PSScriptRoot 'CreWindowsNotification.cs'
    $bytes = [IO.File]::ReadAllBytes($helper)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $sha = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
    if ('CreWindowsNotification.Sender' -as [type]) {
        if (-not ('CreWindowsNotification.Identity' -as [type]) -or [CreWindowsNotification.Identity]::Sha256 -cne $sha) {
            throw 'Cached helper identity differs.'
        }
    } else {
        $source = (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes)
        Add-Type -TypeDefinition ($source + "`nnamespace CreWindowsNotification { public static class Identity { public const string Sha256 = ""$sha""; } }")
    }
    $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
    $configured = [CreWindowsNotification.Sender]::IsConfigured($AlertWebhookFile, $repo)
} catch { $configured = $false }
if (-not $Quiet) {
    [ordered]@{ configured=$configured; scope='private_endpoint_file_only'; networkCalled=$false } | ConvertTo-Json -Compress
}
if ($configured) { exit 0 }
exit 1
