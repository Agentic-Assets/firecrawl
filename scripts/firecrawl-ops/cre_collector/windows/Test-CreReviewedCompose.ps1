# Isolated fake-executable proof for the native Compose completion receipt.
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('cre-reviewed-compose-fixture-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $root
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Invoke-CreReviewedCompose.ps1') -Destination $root
$stub=@'
$script:ownerChecks=0
function ConvertTo-CreHandoffPath([string]$Path) {return $Path}
function Get-CreReviewedControlHash([string]$Path) {
 $h=[Security.Cryptography.SHA256]::Create()
 try {return ([BitConverter]::ToString($h.ComputeHash([IO.File]::ReadAllBytes($Path)))).Replace('-','').ToLowerInvariant()}
 finally {$h.Dispose()}
}
function Read-CreHandoffJson([string]$Text) {return $Text|ConvertFrom-Json}
function Get-CreHandoffHash([string]$Value) {
 $h=[Security.Cryptography.SHA256]::Create()
 try {return ([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-','').ToLowerInvariant()}
 finally {$h.Dispose()}
}
function Assert-CreReviewedOwner($Release,[string]$Control,[string]$Hash,[int]$Owner,[string]$Generation) {
 $script:ownerChecks++
 if($env:CRE_FAKE_OWNER_FAIL -ceq '1' -and $script:ownerChecks -ge 3) {throw 'fixture owner lost'}
}
function Save-CreHandoffExclusive([string]$Path,[string]$Text) {
 $f=[IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
 try {$w=New-Object IO.StreamWriter($f);$w.Write($Text);$w.Flush()} finally {$f.Dispose()}
}
'@
[IO.File]::WriteAllText((Join-Path $root 'CreReviewedTaskUpdate.ps1'),$stub)
$source=@'
using System;
using System.IO;
class FakeDocker {
 public static int Main(string[] args) {
  if (Array.IndexOf(args,"inspect") >= 0) {
   Console.WriteLine("sha256:"+new string('c',64));
   return 0;
  }
  string marker=Environment.GetEnvironmentVariable("CRE_FAKE_MARKER");
  if (!String.IsNullOrEmpty(marker)) File.WriteAllText(marker,"invoked");
  Console.Error.WriteLine("fixture native diagnostic");
  return Environment.GetEnvironmentVariable("CRE_FAKE_EXIT")=="1" ? 7 : 0;
 }
}
'@
$src=Join-Path $root 'fake.cs';$docker=Join-Path $root 'fake.exe';[IO.File]::WriteAllText($src,$source)
$csc=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:exe ("/out:"+$docker) $src
if($LASTEXITCODE -ne 0){throw 'Fake executable compilation failed'}
function Hash([string]$Path) {$h=[Security.Cryptography.SHA256]::Create();try {return ([BitConverter]::ToString($h.ComputeHash([IO.File]::ReadAllBytes($Path)))).Replace('-','').ToLowerInvariant()} finally {$h.Dispose()}}
$control=Join-Path $root 'control.py';[IO.File]::WriteAllText($control,'fixture')
$plan=Join-Path $root 'plan.json';$receipt=Join-Path $root 'receipt.json';$marker=Join-Path $root 'marker.txt'
$config=Join-Path $root 'fixture.yaml';[IO.File]::WriteAllText($config,'fixture')
$imagePlan=Join-Path $root 'image-plan.json'
$selected=@{sha=('a'*40);tree=('b'*40)}
[IO.File]::WriteAllText($imagePlan,(@{selected=$selected}|ConvertTo-Json -Compress))
$env:CRE_FAKE_MARKER=$marker
function RunFixture([int]$Exit,[bool]$LoseOwner,[bool]$BadService) {
 if(Test-Path -LiteralPath $receipt){Remove-Item -LiteralPath $receipt -Force}
 if(Test-Path -LiteralPath $marker){Remove-Item -LiteralPath $marker -Force}
 $env:CRE_FAKE_EXIT=[string]$Exit;$env:CRE_FAKE_OWNER_FAIL=if($LoseOwner){'1'}else{'0'}
 $service=if($BadService){'nuq-postgres'}else{'api'}
 $value=@{release=@{roots=@{native='C:/firecrawl'};wsl=@{}};selected=$selected;
  image_plan=$imagePlan;image_plan_sha256=(Hash $imagePlan);
  files=@(@{path=$config;sha256=(Hash $config)});
  images=@(@{tag=('firecrawl-api:qa-reviewed-'+('a'*24));image=('sha256:'+('c'*64))});
  docker=$docker;docker_sha256=(Hash $docker);argv=@('--host','npipe:////./pipe/dockerDesktopLinuxEngine','compose','--project-name','firecrawl','--project-directory','C:/firecrawl','-f',$config,'up','-d','--no-deps','--no-build','--pull','never',$service)}
 [IO.File]::WriteAllText($plan,($value|ConvertTo-Json -Depth 8 -Compress))
 $launcher=if($PSVersionTable.PSEdition -ceq 'Desktop'){Join-Path $PSHOME 'powershell.exe'}else{Join-Path $PSHOME 'pwsh.exe'}
 $priorPreference=$ErrorActionPreference
 $ErrorActionPreference='Continue'
 & $launcher -NoProfile -NonInteractive -File (Join-Path $root 'Invoke-CreReviewedCompose.ps1') -Plan $plan -PlanSha (Hash $plan) -Control $control -ControlSha (Hash $control) -OwnerPid 123 -Generation 'fixture-generation' -Receipt $receipt 2>$null
 $code=$LASTEXITCODE
 $ErrorActionPreference=$priorPreference
 return $code
}
if((RunFixture 0 $false $false) -ne 0 -or -not (Test-Path -LiteralPath $marker)){throw 'Fake successful process did not complete'}
$r=Get-Content -LiteralPath $receipt -Raw|ConvertFrom-Json
if($r.stage -cne 'ComposeUp' -or -not $r.completed -or $r.docker_exit -ne 0 -or $r.plan_sha256 -cne (Hash $plan)){throw 'Success receipt differs'}
if((RunFixture 1 $false $false) -eq 0){throw 'Fake failed process reported success'}
$r=Get-Content -LiteralPath $receipt -Raw|ConvertFrom-Json
if(-not $r.completed -or $r.docker_exit -ne 7){throw 'Failure receipt differs'}
if((RunFixture 0 $true $false) -eq 0 -or (Test-Path -LiteralPath $receipt) -or -not (Test-Path -LiteralPath $marker)){throw 'Owner loss after invocation must not write a receipt'}
if((RunFixture 0 $false $true) -eq 0 -or (Test-Path -LiteralPath $marker) -or (Test-Path -LiteralPath $receipt)){throw 'Unsupported service must not invoke fake process'}
Write-Output 'CRE reviewed Compose completion fixture passed'
