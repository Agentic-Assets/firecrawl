Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Invoke-CreDispatcherWake.ps1')
function Assert-Wake($condition,[string]$message) { if(-not $condition) { throw $message } }
$localNow=[DateTime]::Today.AddHours(12)
$offset=[TimeZoneInfo]::Local.GetUtcOffset($localNow)
$now=[DateTimeOffset]::new($localNow,$offset)
$today=$localNow.ToString('yyyy-MM-dd')
$xml=[xml]@"
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers>
<CalendarTrigger><StartBoundary>${today}T08:00:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
<CalendarTrigger><StartBoundary>${today}T20:00:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
</Triggers></Task>
"@
$due=Get-CreWakeDue $xml $now
Assert-Wake (-not $due.fallback -and @($due.due).Count -eq 1) 'Only the already due daily trigger should be returned.'
$unsupported=[xml]'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><LogonTrigger /></Triggers></Task>'
$fallback=Get-CreWakeDue $unsupported $now
Assert-Wake ($fallback.fallback -and @($fallback.due).Count -eq 1) 'Unknown trigger must yield a marked current-policy fallback.'
$prior=$localNow.AddDays(-1)
$weeklyStart=$prior.AddDays(-14).ToString('yyyy-MM-dd')
$weekday=$prior.DayOfWeek.ToString()
$weekly=[xml](('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><CalendarTrigger><StartBoundary>'+ $weeklyStart +'T08:00:00</StartBoundary><Enabled>true</Enabled><ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><'+$weekday+' /></DaysOfWeek></ScheduleByWeek></CalendarTrigger></Triggers></Task>'))
$missed=Get-CreWakeDue $weekly $now
Assert-Wake (-not $missed.fallback -and @($missed.due).Count -eq 1) 'A weekly opportunity during an outage must remain visible for current-policy catchup.'
$launcher=Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
$defaults=Get-CreWakeDefaults $launcher
$prefix='-NoProfile -NonInteractive -File "'+$launcher+'" -Tier series -AcknowledgeDatabaseWrites -ExpectedSha '+('a'*40)+' -WorkDeadlineSeconds 1800 '
$serial=ConvertFrom-CreWakeAction ($prefix+'-Source daum-commercial -AdmitDaumVerifiedSubset -DaumSubsetPolicy tenure-and-empty-title-v2 -NotificationMode StartFinish') $launcher ([string]$defaults['LinuxRepo']) $defaults
Assert-Wake (-not $serial.pipeline -and $serial.daum_subset -and $serial.daum_subset_policy -ceq 'tenure-and-empty-title-v2' -and $serial.notification_mode -ceq 'StartFinish') 'Serial DAUM policy must retain subset and notification.'
$pipeline=ConvertFrom-CreWakeAction ($prefix+'-Pipeline -Sources colliers,savills -SourceWorkers 2 -PageCap 0') $launcher ([string]$defaults['LinuxRepo']) $defaults
Assert-Wake ($pipeline.pipeline -and @($pipeline.sources).Count -eq 2 -and $pipeline.workers -eq 2 -and $pipeline.page_cap -eq 0) 'Pipeline policy must retain selected sources and page cap.'
$refused=$false
try { $null=ConvertFrom-CreWakeAction ($prefix+'-Source colliers -UnknownOption value') $launcher ([string]$defaults['LinuxRepo']) $defaults }
catch { $refused=$true }
Assert-Wake $refused 'Unknown action options must be refused.'
$preview=@(& (Join-Path $PSScriptRoot 'Install-CreDispatcherTasks.ps1') -ExpectedSha ('a'*40))
Assert-Wake ($preview.Count -eq 2) 'Preview must contain separate scanner and drain tasks.'
foreach($item in $preview) {
    $doc=[xml]$item.Xml
    Assert-Wake ($doc.Task.Settings.Enabled -ceq 'false') 'Preview must leave each task disabled.'
    Assert-Wake ($doc.Task.Triggers.CalendarTrigger.Repetition.Interval -ceq 'PT5M') 'Preview must repeat periodically.'
    Assert-Wake (([string]$doc.Task.Actions.Exec.Arguments).Contains('-ExpectedSha')) 'Preview must retain the exact code pin.'
}
Write-Output 'CreDispatcherWake synthetic calendar checks passed.'
