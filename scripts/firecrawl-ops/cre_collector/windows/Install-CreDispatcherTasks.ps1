<# Preview by default. Registration is explicit and never replaces existing tasks. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
      [switch]$Register,[switch]$Enable)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if($env:OS -ne 'Windows_NT') {throw 'Windows is required'}
if($Enable -and -not $Register) {throw 'Enable requires registration'}
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$launcher=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'Invoke-CreDispatcherWake.ps1'))
if(-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {throw 'Dispatcher launcher unavailable'}
$candidates=@((Join-Path $env:LOCALAPPDATA 'Programs/PowerShell/7/pwsh.exe'),(Join-Path $env:ProgramFiles 'PowerShell/7/pwsh.exe'))
$exe=@($candidates | Where-Object {Test-Path -LiteralPath $_ -PathType Leaf} | Select-Object -First 1)
if($exe.Count -ne 1) {throw 'PowerShell 7 unavailable'}
if((Get-Item -LiteralPath $exe[0]).VersionInfo.ProductVersion -notmatch '^7[.]') {throw 'Dispatcher task host must be PowerShell 7'}
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$start=[DateTime]::Now.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:00')
function Escape([string]$value) {return [Security.SecurityElement]::Escape($value)}
$definitions=@()
foreach($mode in @('Snapshot','Drain')) {
    $name='Dispatcher '+$mode
    $drain=if($mode -ceq 'Drain'){' "-DrainOnly"'}else{''}
    $arguments='"-NoProfile" "-NonInteractive" "-File" "'+$launcher+'" "-ExpectedSha" "'+$ExpectedSha+'"'+$drain
    $xml=@"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Author>Agentic Assets</Author><Description>CRE dispatcher $mode at reviewed SHA $ExpectedSha.</Description></RegistrationInfo>
 <Triggers><CalendarTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled><Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
 <Principals><Principal id="Author"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>false</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>false</Enabled><Hidden>false</Hidden><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority></Settings>
 <Actions Context="Author"><Exec><Command>$(Escape $exe[0])</Command><Arguments>$(Escape $arguments)</Arguments><WorkingDirectory>$(Escape $repo)</WorkingDirectory></Exec></Actions>
</Task>
"@
    $definitions+=@([pscustomobject]@{TaskPath='\Agentic Assets\CRE\';TaskName=$name;Xml=$xml})
}
if(-not $Register) {return $definitions}
. (Join-Path $PSScriptRoot 'CreTaskContract.ps1')
$existing=@(Get-ScheduledTask -ErrorAction Stop)
foreach($definition in $definitions) {
    if(@($existing | Where-Object {$_.TaskPath -ieq $definition.TaskPath -and $_.TaskName -ieq $definition.TaskName}).Count) {throw 'Dispatcher task already exists'}
}
$service=New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder=$service.GetFolder('\Agentic Assets\CRE')
foreach($definition in $definitions) {
    # TASK_CREATE refuses a race with another installer; never replace a task.
    [void]$folder.RegisterTask($definition.TaskName,$definition.Xml,2,$sid,$null,3,$null)
    $readback=Export-ScheduledTask -TaskPath $definition.TaskPath -TaskName $definition.TaskName -ErrorAction Stop
    if((Get-CreTaskContract $readback) -cne (Get-CreTaskContract $definition.Xml)) {
        [void](Disable-ScheduledTask -TaskPath $definition.TaskPath -TaskName $definition.TaskName -ErrorAction Stop)
        throw 'Dispatcher task readback differs; task disabled'
    }
}
if($Enable) {
    foreach($definition in $definitions) {
        try {
            [void](Enable-ScheduledTask -TaskPath $definition.TaskPath -TaskName $definition.TaskName -ErrorAction Stop)
            $readback=Export-ScheduledTask -TaskPath $definition.TaskPath -TaskName $definition.TaskName -ErrorAction Stop
            $expected=$definition.Xml.Replace('<Enabled>false</Enabled><Hidden>','<Enabled>true</Enabled><Hidden>')
            if((Get-CreTaskContract $readback) -cne (Get-CreTaskContract $expected)) {throw 'Dispatcher task enable readback differs'}
        } catch {
            [void](Disable-ScheduledTask -TaskPath $definition.TaskPath -TaskName $definition.TaskName -ErrorAction Stop)
            throw
        }
    }
}
return $definitions
