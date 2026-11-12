$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$launcher = Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Launcher does not parse.' }
$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -ceq 'Get-CreLocalSeriesSettlement' }, $true)
if ($null -eq $fn) { throw 'Reader function missing.' }
. ([scriptblock]::Create($fn.Extent.Text))

Add-Type -TypeDefinition @'
using System.IO;
namespace CreWindowsCapture {
  public class StreamResult {
    public bool Eof = true, Truncated = false, RunReferenceConflict = false;
    public long ObservedBytes = 0;
    public string ObservedRunDirectory;
  }
  public class CaptureResult {
    public bool Started = true, Failed = false;
    public int? NativeExitCode = 0;
    public StreamResult Stdout = new StreamResult(), Stderr = new StreamResult();
  }
  public static class Capture {
    public static string Payload;
    public static bool BadReaderEof;
    public static int Calls;
    public static CaptureResult RunReadOnly(string exe, string[] args, string output, string error, int deadline) {
      Calls++;
      File.WriteAllText(output, Payload);
      File.WriteAllText(error, "");
      var result = new CaptureResult();
      result.Stdout.ObservedBytes = System.Text.Encoding.UTF8.GetByteCount(Payload);
      result.Stdout.Eof = !BadReaderEof;
      return result;
    }
  }
}
'@

function Assert-True([bool]$condition, [string]$message) { if (-not $condition) { throw $message } }

$Tier = 'series'; $Pipeline = $true
$ExpectedSha = 'a' * 40
$LinuxRepo = '/home/test/firecrawl'
$Distro = 'test'; $LinuxUser = 'test'
$selectedSourceKeys = @('franklin-street')
$auditDir = Join-Path ([IO.Path]::GetTempPath()) ('cre-local-settlement-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($auditDir)
try {
    $directory = "$LinuxRepo/scripts/firecrawl-ops/cre_collector/out/checkpoint-series/2026-11-12T003159Z"
    $capture = New-Object CreWindowsCapture.CaptureResult
    $capture.Stdout.ObservedRunDirectory = $directory
    $capture.NativeExitCode = 23
    $record = [ordered]@{
        status='returned'; nativeExitCode=23; capture=$capture
        windowsAdmissionUnixMs=1000; windowsInvocationId=('e' * 32)
        linuxSettlement='unverified'
    }
    $proof = @{
        schema_version=1; scope='owned_local_processes_only'; local_settlement='confirmed'
        collector_git_sha=$ExpectedSha; windows_admission_unix_ms=1000
        windows_invocation_id=$record.windowsInvocationId
        series_id='2026-11-12T003159Z'; invocation_id=('b' * 32)
        pipeline=$true; source_keys=@('franklin-street'); terminal_status='failed'
    }
    [CreWindowsCapture.Capture]::Payload = ($proof | ConvertTo-Json -Compress)
    $observed = Get-CreLocalSeriesSettlement $record 'fixture'
    Assert-True ($null -ne $observed -and $observed.terminal_status -ceq 'failed') 'A failed operation can have locally settled processes.'
    Assert-True ($record.linuxSettlement -ceq 'unverified') 'Original remote/overall field must remain unchanged.'
    $proof.terminal_status = 'complete_with_source_failures'
    [CreWindowsCapture.Capture]::Payload = ($proof | ConvertTo-Json -Compress)
    $partial = Get-CreLocalSeriesSettlement $record 'source-failures'
    Assert-True ($null -ne $partial -and $partial.terminal_status -ceq 'complete_with_source_failures') 'Settled source failures must retain the terminal status.'
    # KNOWN BLOCKER: simulate a changed post-launch verifier returning a forged
    # otherwise matching success. This demonstrates the missing byte anchor;
    # the candidate remains draft and must not be deployed while it passes.
    $proof.terminal_status = 'complete'
    [CreWindowsCapture.Capture]::Payload = ($proof | ConvertTo-Json -Compress)
    $swapped = Get-CreLocalSeriesSettlement $record 'known-code-swap'
    Assert-True ($null -ne $swapped -and $swapped.terminal_status -ceq 'complete') 'Known verifier-swap defect must stay visible pending a trusted verifier anchor.'
    $prior = [CreWindowsCapture.Capture]::Calls
    $capture.Stdout.Eof = $false
    Assert-True ($null -eq (Get-CreLocalSeriesSettlement $record 'eof')) 'Missing original EOF must refuse.'
    Assert-True ([CreWindowsCapture.Capture]::Calls -eq $prior) 'No receipt read after bad capture.'
    $capture.Stdout.Eof = $true
    $capture.Stdout.Truncated = $true
    Assert-True ($null -eq (Get-CreLocalSeriesSettlement $record 'truncated')) 'Truncated original capture must refuse.'
    $capture.Stdout.Truncated = $false
    $record.nativeExitCode = $null
    Assert-True ($null -eq (Get-CreLocalSeriesSettlement $record 'exit')) 'Missing native return must refuse.'
    $record.nativeExitCode = 23
    [CreWindowsCapture.Capture]::BadReaderEof = $true
    Assert-True ($null -eq (Get-CreLocalSeriesSettlement $record 'reader-eof')) 'Reader EOF must be observed.'
    [CreWindowsCapture.Capture]::BadReaderEof = $false
    $proof.windows_invocation_id = 'f' * 32
    [CreWindowsCapture.Capture]::Payload = ($proof | ConvertTo-Json -Compress)
    Assert-True ($null -eq (Get-CreLocalSeriesSettlement $record 'wrong-id')) 'Cross-invocation result must refuse.'
    $Tier = 'enrich'
    Assert-True ($null -eq (Get-CreLocalSeriesSettlement $record 'enrich')) 'Enrichment is outside this proof.'
    Write-Output 'Local settlement launcher fixture passed.'
} finally {
    Remove-Item -LiteralPath $auditDir -Recurse -Force
}
