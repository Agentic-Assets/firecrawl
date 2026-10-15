<# Actual disposable loopback HTTP only. No Docker, WSL, providers or database.
   Run in a fresh PowerShell 7 or Windows PowerShell 5.1 -NoProfile process. #>
[CmdletBinding()]
param([string]$ProofDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows native proof only.' }
if (-not $ProofDirectory) { $ProofDirectory=Join-Path $PSScriptRoot ('../../../../.scratch/local-api-readiness-'+[Guid]::NewGuid().ToString('N')) }
[void](New-Item -ItemType Directory -Path $ProofDirectory -Force)
$ProofDirectory=[IO.Path]::GetFullPath($ProofDirectory)
$creLauncher=Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
$creSource=[IO.File]::ReadAllText($creLauncher)
$creTokens=$null; $creErrors=$null
$creAst=[Management.Automation.Language.Parser]::ParseInput($creSource,[ref]$creTokens,[ref]$creErrors)
if ($creErrors.Count) { throw 'Launcher must parse.' }
$creFunction=@($creAst.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Wait-CreLocalApi' },$true))
if ($creFunction.Count -ne 1) { throw 'Expected exactly one readiness function.' }
. ([scriptblock]::Create($creFunction[0].Extent.Text))
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
public sealed class CreReadinessHttpFixture : IDisposable {
    readonly TcpListener listener;
    readonly Thread worker;
    readonly ManualResetEvent stop = new ManualResetEvent(false);
    readonly List<string> headers = new List<string>();
    readonly string mode, location;
    int requests;
    public int Port { get; private set; }
    public int Requests { get { return Interlocked.CompareExchange(ref requests,0,0); } }
    public bool WorkerAlive { get { return worker.IsAlive; } }
    public string[] Headers { get { lock(headers) return headers.ToArray(); } }
    public CreReadinessHttpFixture(string mode, string location) {
        this.mode=mode; this.location=location;
        listener=new TcpListener(IPAddress.Loopback,0); listener.Start();
        Port=((IPEndPoint)listener.LocalEndpoint).Port;
        worker=new Thread(Serve); worker.IsBackground=true; worker.Start();
    }
    void Serve() {
        while(!stop.WaitOne(0)) {
            try {
                using(var client=listener.AcceptTcpClient()) {
                    client.ReceiveTimeout=500; client.SendTimeout=500;
                    using(var stream=client.GetStream()) {
                        var bytes=new List<byte>();
                        while(bytes.Count<8192) {
                            int value=stream.ReadByte(); if(value<0) break; bytes.Add((byte)value);
                            int n=bytes.Count;
                            if(n>=4 && bytes[n-4]==13 && bytes[n-3]==10 && bytes[n-2]==13 && bytes[n-1]==10) break;
                        }
                        lock(headers) headers.Add(Encoding.ASCII.GetString(bytes.ToArray()));
                        int seen=Interlocked.Increment(ref requests);
                        if(mode=="hang") { stop.WaitOne(600); continue; }
                        if(mode=="drop") continue;
                        int status=mode=="redirect"?302:mode=="unauthorized"?401:mode=="error"?500:mode=="delayed" && seen<=2?503:200;
                        string extra=mode=="redirect"?"Location: "+location+"\r\n":mode=="unauthorized"?"WWW-Authenticate: Basic realm=\"fixture\"\r\n":"";
                        if(mode=="delayed") extra+="Set-Cookie: synthetic=must-not-be-reused\r\n";
                        string length=mode=="body"?"10000":"0";
                        byte[] response=Encoding.ASCII.GetBytes("HTTP/1.1 "+status+" Fixture\r\n"+extra+"Content-Length: "+length+"\r\nConnection: close\r\n\r\n");
                        stream.Write(response,0,response.Length); stream.Flush();
                        if(mode=="body") stop.WaitOne(600);
                    }
                }
            } catch(SocketException) { if(stop.WaitOne(0)) break; }
              catch(IOException) { if(stop.WaitOne(0)) break; }
              catch(ObjectDisposedException) { break; }
        }
    }
    public void Dispose() {
        stop.Set(); listener.Stop();
        if(!worker.Join(2000)) throw new InvalidOperationException("Owned fixture thread did not settle");
        stop.Dispose();
    }
}
'@
$creChecks=0
$creResults=New-Object 'System.Collections.Generic.List[object]'
function Assert-CreReadiness($Condition,[string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:creChecks++
}
$creDefaults=@{}
foreach ($creParameter in $creFunction[0].Body.ParamBlock.Parameters) {
    $creDefaults[$creParameter.Name.VariablePath.UserPath]=$creParameter.DefaultValue.SafeGetValue()
}
Assert-CreReadiness ($creDefaults.BudgetSeconds -eq 90 -and $creDefaults.RequestTimeoutMilliseconds -eq 2000 -and $creDefaults.Endpoint -ceq 'http://127.0.0.1:3002/') 'Production defaults must retain the fixed endpoint and 90-second/2-second bounds.'
$creProductionCalls=@($creAst.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -ceq 'Wait-CreLocalApi' },$true))
Assert-CreReadiness ($creProductionCalls.Count -eq 1 -and $creProductionCalls[0].CommandElements.Count -eq 1) 'The actual launcher must use fixed readiness defaults without a public endpoint override.'
foreach ($creOverrides in @(@{BudgetSeconds=91},@{RequestTimeoutMilliseconds=2001},@{RetryDelayMilliseconds=1001})) {
    $creRefused=$false
    try { Wait-CreLocalApi @creOverrides | Out-Null } catch { $creRefused=$true }
    Assert-CreReadiness $creRefused 'Out-of-bound probe budgets must refuse before network activity.'
}
foreach ($creCase in @(
    @{Mode='healthy'; Ready=$true; Status=200; Minimum=1},
    @{Mode='delayed'; Ready=$true; Status=200; Minimum=3},
    @{Mode='error'; Ready=$false; Status=500; Minimum=2},
    @{Mode='unauthorized'; Ready=$false; Status=401; Minimum=2},
    @{Mode='drop'; Ready=$false; Status=$null; Minimum=2},
    @{Mode='hang'; Ready=$false; Status=$null; Minimum=1},
    @{Mode='body'; Ready=$true; Status=200; Minimum=1}
)) {
    $creServer=New-Object CreReadinessHttpFixture($creCase.Mode,'')
    $creClock=[Diagnostics.Stopwatch]::StartNew()
    try {
        $creBudget=if ($creCase.Ready) { 2 } else { 0.35 }
        $creResult=Wait-CreLocalApi -Endpoint ('http://127.0.0.1:'+$creServer.Port+'/') -BudgetSeconds $creBudget -RequestTimeoutMilliseconds 120 -RetryDelayMilliseconds 25
        $creClock.Stop()
        Assert-CreReadiness ($creResult.ready -eq $creCase.Ready -and $creResult.lastHttpStatus -eq $creCase.Status) ('Actual HTTP outcome: '+$creCase.Mode)
        Assert-CreReadiness ($creServer.Requests -ge $creCase.Minimum) ('Actual HTTP requests: '+$creCase.Mode)
        Assert-CreReadiness ($creClock.Elapsed.TotalSeconds -lt ($creBudget+1)) ('Finite HTTP wait: '+$creCase.Mode)
        Assert-CreReadiness ($creResult.requestTimeoutMilliseconds -eq 120 -and $creResult.budgetSeconds -eq $creBudget) 'Evidence must retain the real probe budget.'
        foreach ($creHeaders in $creServer.Headers) {
            Assert-CreReadiness ($creHeaders -match '^GET / HTTP/1[.]1' -and $creHeaders -notmatch '(?im)^(Authorization|Proxy-Authorization|Cookie):') 'Only anonymous root GET requests are permitted.'
        }
        $creResults.Add([pscustomobject]@{ mode=$creCase.Mode; result=$creResult; requests=$creServer.Requests; wallMilliseconds=[Math]::Round($creClock.Elapsed.TotalMilliseconds) })
    } finally { $creServer.Dispose() }
    Assert-CreReadiness (-not $creServer.WorkerAlive) 'Every owned HTTP fixture thread must settle.'
}
$creDestination=New-Object CreReadinessHttpFixture('healthy','')
$creRedirect=New-Object CreReadinessHttpFixture('redirect',('http://127.0.0.1:'+$creDestination.Port+'/'))
try {
    $creResult=Wait-CreLocalApi -Endpoint ('http://127.0.0.1:'+$creRedirect.Port+'/') -BudgetSeconds 0.3 -RequestTimeoutMilliseconds 120 -RetryDelayMilliseconds 25
    Assert-CreReadiness (-not $creResult.ready -and $creResult.lastHttpStatus -eq 302 -and $creRedirect.Requests -gt 0 -and $creDestination.Requests -eq 0) 'A real redirect must never be followed, even to another loopback server.'
    $creResults.Add([pscustomobject]@{ mode='redirect'; result=$creResult; requests=$creRedirect.Requests; destinationRequests=$creDestination.Requests })
} finally { $creRedirect.Dispose(); $creDestination.Dispose() }
Assert-CreReadiness (-not $creRedirect.WorkerAlive -and -not $creDestination.WorkerAlive) 'Both redirect fixture threads must settle.'
foreach ($creEndpoint in @('https://127.0.0.1/','http://localhost/','http://example.invalid/','http://user:password@127.0.0.1/','http://127.0.0.1/path','http://127.0.0.1/?query','http://127.0.0.1/#fragment')) {
    $creRefused=$false
    try { Wait-CreLocalApi -Endpoint $creEndpoint -BudgetSeconds 0.1 | Out-Null } catch { $creRefused=$true }
    Assert-CreReadiness $creRefused 'Invalid probe destination must refuse before network activity.'
}
$creSummary=[ordered]@{ status='passed'; assertions=$creChecks; powershellVersion=$PSVersionTable.PSVersion.ToString();
    launcherSha256=(Get-FileHash -LiteralPath $creLauncher -Algorithm SHA256).Hash.ToLowerInvariant();
    providerOrDatabaseCalled=$false; dockerOrWslChanged=$false; allFixtureThreadsSettled=$true; results=$creResults.ToArray() }
$creSummary | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath (Join-Path $ProofDirectory 'readiness.json') -Encoding UTF8
Write-Output ('Local API readiness PASS: '+$creChecks+' assertions; proof='+$ProofDirectory)
