<#
.SYNOPSIS
Preview or register one visible Windows task for the existing guarded CRE launcher.
.DESCRIPTION
Preview is the default and starts no task, WSL, Docker, provider or database call.
-Register creates a disabled task. Adding -Enable enables only that newly created
task after exact definition readback. Existing tasks are never overwritten.
Linux owns the finite deadline, recovery fence and cooperative process settlement.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\z')][string]$TaskName,
    [Parameter(Mandatory = $true)]
    [ValidateSet('monitor', 'enrich', 'series')][string]$Tier,
    [ValidatePattern('^[a-z0-9][a-z0-9-]*\z')][string]$Source,
    [ValidatePattern('^[a-z0-9][a-z0-9-]*(,[a-z0-9][a-z0-9-]*)*$')][string]$Sources,
    [switch]$Pipeline,
    [ValidateRange(1, 4)][int]$SourceWorkers,
    [ValidatePattern('^[a-z0-9][a-z0-9-]*(,[a-z0-9][a-z0-9-]*)*$')][string]$AdmitCurrentInventorySources,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}\z')][string]$ExpectedSha,
    [ValidateRange(1, 50)][int]$Batch = 50,
    [ValidateRange(30, 14400)][int]$DeadlineSeconds = 1800,
    [ValidateRange(30, 86400)][int]$WorkDeadlineSeconds,
    [switch]$AcknowledgeDatabaseWrites,
    [ValidateSet('interra-realty','faris-lee','fortis-net-lease','unique-properties','kiser-group','pinnacle-rea','cawley-chicago','bradford-allen','hudson-peters','gibson-commercial','leibsohn','nai-hiffman','nai-martens','bull-realty','tri-commercial','berger-commercial','nai-bergman','nai-isaac','trinity-partners','metro-commercial','33-realty','nai-hallmark','nai-plotkin','greysteel','nai-talcor','nai-dominion')][string]$AdmitCurrentInventorySource,
    [switch]$Apply,
    [string]$AlertWebhookFile,
    [ValidateSet('Failure','StartFinish')][string]$NotificationMode = 'Failure',
    [ValidatePattern('^(?:[01][0-9]|2[0-3]):[0-5][0-9](?:,(?:[01][0-9]|2[0-3]):[0-5][0-9]){0,3}\z')]
    [string]$DailyAt = '08:00,20:00',
    [ValidateSet('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')]
    [string]$WeeklyOn,
    [switch]$Register,
    [switch]$Enable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this installer on Windows.' }

# Pure selection validation runs before readiness, telemetry or notification work.
if ($Sources -and ($Source -or -not $Pipeline -or $Tier -ne 'series')) {
    throw '-Sources requires pipeline series and conflicts with -Source.'
}
if ($Pipeline -and $Tier -ne 'series') { throw '-Pipeline requires series.' }
if ($PSBoundParameters.ContainsKey('SourceWorkers') -and -not $Pipeline) {
    throw '-SourceWorkers requires -Pipeline.'
}
$resolvedSourceWorkers = 1
if ($Pipeline) {
    $resolvedSourceWorkers = 2
    if ($PSBoundParameters.ContainsKey('SourceWorkers')) { $resolvedSourceWorkers = $SourceWorkers }
}
$knownSourceKeys = @()
if ($Pipeline) {
$policyForSelection = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../data/cre-source-policy.json') -Raw | ConvertFrom-Json
$knownSourceKeys = @($policyForSelection.PSObject.Properties.Name)
}
$selectedSourceKeys = @()
if ($Sources) {
    $selectedSourceKeys = @($Sources.Split(','))
    if (@($selectedSourceKeys | Sort-Object -Unique).Count -ne $selectedSourceKeys.Count) { throw 'Duplicate source keys.' }
    foreach ($key in $selectedSourceKeys) {
        if ($knownSourceKeys -cnotcontains $key) { throw 'Unknown source key.' }
    }
} elseif ($Source -ceq 'all' -and $Pipeline) { $selectedSourceKeys = $knownSourceKeys }
elseif ($Source) { $selectedSourceKeys = @($Source) }
if ($AdmitCurrentInventorySource -and $AdmitCurrentInventorySources) { throw 'Conflicting admission flags.' }
$selectedAdmissionKeys = @()
if ($AdmitCurrentInventorySources) {
    if (-not $Pipeline) { throw 'Multiple admissions require pipeline mode.' }
    $selectedAdmissionKeys = @($AdmitCurrentInventorySources.Split(','))
} elseif ($AdmitCurrentInventorySource) { $selectedAdmissionKeys = @($AdmitCurrentInventorySource) }
if (@($selectedAdmissionKeys | Sort-Object -Unique).Count -ne $selectedAdmissionKeys.Count) { throw 'Duplicate admission keys.' }
$admissibleKeys = $selectedAdmissionKeys
if ($AdmitCurrentInventorySources) {
$buildoutRows = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../data/cre-buildout-sources.json') -Raw | ConvertFrom-Json
$admissibleKeys = @('interra-realty') + @($buildoutRows | ForEach-Object { $_[0] })
}
foreach ($key in $selectedAdmissionKeys) {
    if ($Tier -ne 'series' -or $admissibleKeys -cnotcontains $key -or $selectedSourceKeys -cnotcontains $key) {
        throw 'Admission requires an exact selected and supported source key.'
    }
}
$selectedSourceArgument = if ($Sources) { $Sources } else { $Source }

if ($ExpectedSha -cnotmatch '^[0-9a-f]{40}\z') { throw 'ExpectedSha must be a full lowercase commit SHA.' }
if ($Enable -and -not $Register) { throw '-Enable requires -Register; existing tasks are never changed.' }
$Tier = $Tier.ToLowerInvariant()
if ($WeeklyOn) { $WeeklyOn = [Globalization.CultureInfo]::InvariantCulture.TextInfo.ToTitleCase($WeeklyOn.ToLowerInvariant()) }

# Source names come from the existing JSON policy, without importing a worker.
$policy = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../data/cre-source-policy.json') -Raw | ConvertFrom-Json
$sourceKeys = @($policy.PSObject.Properties.Name)
if (($selectedSourceArgument -ceq 'all' -and $Tier -ne 'series') -or
    (-not $Sources -and $selectedSourceArgument -cne 'all' -and $sourceKeys -cnotcontains $selectedSourceArgument)) {
    throw 'Source is not supported by this tier.'
}
if ($Tier -eq 'monitor' -and $selectedSourceArgument -in @('jll','jll-investor','cbre-dealflow','colliers')) {
    throw 'Source is excluded from the guarded monitor tier.'
}
if ($Tier -eq 'series') {
    if (-not $PSBoundParameters.ContainsKey('WorkDeadlineSeconds') -or -not $AcknowledgeDatabaseWrites) {
        throw 'Series requires an explicit work budget and database-write acknowledgement.'
    }
    foreach ($name in @('Apply','Batch','DeadlineSeconds')) {
        if ($PSBoundParameters.ContainsKey($name)) { throw 'Incremental option supplied for series.' }
    }
} elseif ($PSBoundParameters.ContainsKey('WorkDeadlineSeconds') -or $AcknowledgeDatabaseWrites) {
    throw 'Series option supplied for incremental tier.'
}
if ($Tier -eq 'enrich' -and $selectedSourceArgument -ceq 'matthews' -and $Apply -and $DeadlineSeconds -gt 600) {
    throw 'Applied Matthews enrichment requires a deadline of at most 600 seconds.'
}
if ($NotificationMode -eq 'StartFinish' -and -not $AlertWebhookFile) {
    throw 'Lifecycle notification requires a private webhook-file reference.'
}
if ($AlertWebhookFile -and ($AlertWebhookFile -notmatch '^[A-Za-z]:[\\/]' -or
    $AlertWebhookFile -match '[\r\n\x00-\x1f<>"|?*]' -or $AlertWebhookFile.Substring(2).Contains(':'))) {
    throw 'Webhook reference must be an absolute local Windows file path.'
}
$times = @($DailyAt.Split(','))
if (@($times | Sort-Object -Unique).Count -ne $times.Count) { throw 'Schedule times must be unique.' }

function ConvertTo-CreNativeArgument([string]$Value) {
    # CommandLineToArgvW quoting; the scheduled action uses -File, never -Command.
    return '"' + ([regex]::Replace($Value, '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}
function ConvertTo-CreXml([string]$Value) { return [Security.SecurityElement]::Escape($Value) }
function Register-CreTaskCreateOnly([string]$Name, [string]$Path, [string]$Xml, [string]$UserSid) {
    # TASK_CREATE (2), never TASK_CREATE_OR_UPDATE (6): an atomic collision
    # refusal even when another installer races our preceding enumeration.
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')
    foreach ($part in @($Path.Trim('\').Split('\') | Where-Object { $_ })) {
        try { $folder = $folder.GetFolder($part) }
        catch {
            if ($_.Exception.GetBaseException().HResult -ne -2147024894) { throw }
            try { $folder = $folder.CreateFolder($part, $null) }
            catch { $folder = $folder.GetFolder($part) }
        }
    }
    [void]$folder.RegisterTask($Name, $Xml, 2, $UserSid, $null, 3, $null)
}
function Get-CreTaskContract([string]$Xml) {
    $doc = New-Object Xml.XmlDocument
    $doc.XmlResolver = $null
    $doc.LoadXml($Xml)
    $ns = New-Object Xml.XmlNamespaceManager($doc.NameTable)
    $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    if ($doc.DocumentElement.LocalName -cne 'Task' -or
        $doc.DocumentElement.NamespaceURI -cne $ns.LookupNamespace('t') -or
        $doc.SelectNodes('/t:Task/t:Actions/*', $ns).Count -ne 1 -or
        $doc.SelectNodes('/t:Task/t:Actions/t:Exec', $ns).Count -ne 1 -or
        $doc.SelectNodes('/t:Task/t:Principals/t:Principal', $ns).Count -ne 1) {
        throw 'Registered task has an unexpected execution or principal shape.'
    }
    # Ignore only scheduler-added registration metadata and XML schema version.
    # All action, trigger, principal and settings leaves/attributes remain exact.
    foreach ($node in @($doc.SelectNodes('/t:Task/t:RegistrationInfo/*', $ns))) {
        if ($node.LocalName -in @('Date','URI','SecurityDescriptor')) { [void]$node.ParentNode.RemoveChild($node) }
    }
    # Task Scheduler omits these documented/default values on export. Restore
    # defaults for comparison, while preserving any explicitly different value.
    foreach ($entry in @(
        @('/t:Task/t:Principals/t:Principal','RunLevel','LeastPrivilege'),
        @('/t:Task/t:Settings','Enabled','true'),
        @('/t:Task/t:Settings','AllowStartOnDemand','true'),
        @('/t:Task/t:Settings','Hidden','false'),
        @('/t:Task/t:Settings','RunOnlyIfIdle','false'),
        @('/t:Task/t:Settings','WakeToRun','false'),
        @('/t:Task/t:Settings','RunOnlyIfNetworkAvailable','false'),
        @('/t:Task/t:Settings','Priority','7'),
        @('/t:Task/t:Triggers/t:CalendarTrigger','Enabled','true')
    )) {
        foreach ($parent in @($doc.SelectNodes($entry[0], $ns))) {
            if (-not $parent.SelectSingleNode(('t:'+$entry[1]), $ns)) {
                $element = $doc.CreateElement($entry[1], $ns.LookupNamespace('t'))
                $element.InnerText = $entry[2]
                [void]$parent.AppendChild($element)
            }
        }
    }
    $items = New-Object 'System.Collections.Generic.List[string]'
    function Add-ContractNode($Node, [string]$Path) {
        foreach ($attribute in $Node.Attributes) {
            if ($attribute.Prefix -ceq 'xmlns' -or $attribute.Name -ceq 'xmlns' -or
                ($Path -ceq '/Task' -and $attribute.Name -ceq 'version')) { continue }
            $items.Add($Path + '/@' + $attribute.Name + '=' + $attribute.Value)
        }
        $children = @($Node.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })
        if ($children.Count -eq 0) { $items.Add($Path + '=' + $Node.InnerText) }
        $seen = @{}
        foreach ($child in $children) {
            $key = $child.LocalName
            if (-not $seen.ContainsKey($key)) { $seen[$key] = 0 }
            $seen[$key]++
            Add-ContractNode $child ($Path + '/' + $key + '[' + $seen[$key] + ']')
        }
    }
    Add-ContractNode $doc.DocumentElement '/Task'
    return ($items | Sort-Object) -join [Environment]::NewLine
}

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'))
$powerShell = @(
    (Join-Path $env:LOCALAPPDATA 'Programs/PowerShell/7/pwsh.exe'),
    (Join-Path $env:ProgramFiles 'PowerShell/7/pwsh.exe')
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $powerShell -or -not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw 'Installed PowerShell 7 and the guarded launcher are required.'
}
if ((Get-Item -LiteralPath $powerShell).VersionInfo.ProductVersion -notmatch '^7[.]') {
    throw 'The installed task host must be PowerShell 7.'
}
$arguments = @('-NoProfile','-NonInteractive','-File',$launcher,
    '-Tier',$Tier)
if ($Sources) { $arguments += @('-Sources',$Sources) } else { $arguments += @('-Source',$selectedSourceArgument) }
$arguments += @('-ExpectedSha',$ExpectedSha)
if ($Pipeline) { $arguments += @('-Pipeline','-SourceWorkers',"$resolvedSourceWorkers") }
if ($AdmitCurrentInventorySources) { $arguments += @('-AdmitCurrentInventorySources',$AdmitCurrentInventorySources) }
if ($Tier -eq 'series') {
    $arguments += @('-WorkDeadlineSeconds',"$WorkDeadlineSeconds",'-AcknowledgeDatabaseWrites')
    if ($AdmitCurrentInventorySource) { $arguments += @('-AdmitCurrentInventorySource',$AdmitCurrentInventorySource) }
} else {
    $arguments += @('-Batch',"$Batch",'-DeadlineSeconds',"$DeadlineSeconds")
    if ($Apply) { $arguments += '-Apply' }
}
if ($AlertWebhookFile) { $arguments += @('-AlertWebhookFile',$AlertWebhookFile,'-NotificationMode',$NotificationMode) }
$actionArguments = ($arguments | ForEach-Object { ConvertTo-CreNativeArgument $_ }) -join ' '
$triggerXml = foreach ($time in $times) {
    $start = [DateTime]::Today.ToString('yyyy-MM-dd') + 'T' + $time + ':00'
    $schedule = if ($WeeklyOn) {
        '<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><' + $WeeklyOn + '/></DaysOfWeek></ScheduleByWeek>'
    } else { '<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>' }
    '<CalendarTrigger><StartBoundary>' + $start + '</StartBoundary><Enabled>true</Enabled>' + $schedule + '</CalendarTrigger>'
}
$description = "Guarded CRE $Tier for $selectedSourceArgument at reviewed SHA $ExpectedSha. Linux owns its finite deadline and settlement. Inspect out/windows-launcher audits; use the exact run/series cooperative stop helper. Disabling this task prevents future launches; End Task is not settlement proof."
$taskXml = @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>Agentic Assets</Author><Description>$(ConvertTo-CreXml $description)</Description></RegistrationInfo>
  <Triggers>$($triggerXml -join '')</Triggers>
  <Principals><Principal id="Author"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><Duration>PT10M</Duration><WaitTimeout>PT1H</WaitTimeout><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>false</Enabled><Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>$(ConvertTo-CreXml $powerShell)</Command><Arguments>$(ConvertTo-CreXml $actionArguments)</Arguments><WorkingDirectory>$(ConvertTo-CreXml $repo)</WorkingDirectory></Exec></Actions>
</Task>
"@
$taskPath = '\Agentic Assets\CRE\'
if (-not $Register) { Write-Output $taskXml; return }
if ($NotificationMode -eq 'StartFinish') {
    & (Join-Path $PSScriptRoot 'Test-CreNotificationConfig.ps1') -AlertWebhookFile $AlertWebhookFile -Quiet
    if ($LASTEXITCODE -ne 0) { throw 'Private notification configuration is invalid; no task registered.' }
}

# A failed enumeration is not evidence that the task does not exist.
$existing = @(Get-ScheduledTask -ErrorAction Stop |
    Where-Object { $_.TaskPath -ieq $taskPath -and $_.TaskName -ieq $TaskName })
if ($existing.Count -ne 0) { throw 'Task already exists; no existing definition was changed.' }
$expected = Get-CreTaskContract $taskXml
Register-CreTaskCreateOnly $TaskName $taskPath $taskXml $sid
try {
    $registered = Export-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction Stop
    if ((Get-CreTaskContract $registered) -cne $expected) { throw 'Registered task definition differs.' }
} catch {
    [void](Disable-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction Stop)
    throw 'Registration readback failed; task disabled for inspection.'
}
if ($Enable) {
    try {
        [void](Enable-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction Stop)
        $enabledXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction Stop
        $enabledExpected = $taskXml.Replace('<Enabled>false</Enabled><Hidden>', '<Enabled>true</Enabled><Hidden>')
        if ((Get-CreTaskContract $enabledXml) -cne (Get-CreTaskContract $enabledExpected)) {
            throw 'Enabled task definition differs.'
        }
    } catch {
        [void](Disable-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction Stop)
        throw 'Enable or its readback failed; task disabled for inspection.'
    }
}
[ordered]@{ taskPath=$taskPath; taskName=$TaskName; enabled=[bool]$Enable; expectedSha=$ExpectedSha;
    installedOnly=$true; pipelineRunVerified=$false } | ConvertTo-Json
