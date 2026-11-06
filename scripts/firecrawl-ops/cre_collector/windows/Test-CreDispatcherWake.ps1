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
Write-Output 'CreDispatcherWake synthetic calendar checks passed.'
