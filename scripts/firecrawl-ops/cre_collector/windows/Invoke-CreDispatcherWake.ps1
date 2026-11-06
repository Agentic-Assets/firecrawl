<# Uninstalled WIP: discover current series tasks and submit due policy snapshots. #>
[CmdletBinding()]
param([string]$VerifyTaskPath,[string]$VerifyTaskName,[string]$ExpectedTaskSha256,[switch]$DrainOnly,
      [ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'CreReviewedTaskUpdate.ps1')

function Get-CreWakeHash([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-CreWakeDefaults([string]$Launcher) {
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($Launcher,[ref]$tokens,[ref]$errors)
    if (@($errors).Count -ne 0 -or $null -eq $ast.ParamBlock) { throw 'Launcher metadata unavailable' }
    $defaults=@{}
    foreach($parameter in @($ast.ParamBlock.Parameters)) {
        if($null -ne $parameter.DefaultValue) {
            try { $defaults[[string]$parameter.Name.VariablePath.UserPath]=$parameter.DefaultValue.SafeGetValue() }
            catch { throw 'Launcher default is not constant' }
        }
    }
    return $defaults
}

function Get-CreWakeDue([xml]$Document, [DateTimeOffset]$Now) {
    # Support the installer's daily/weekly triggers. Unknown or ambiguous
    # calendars get one clearly marked current-policy catchup per UTC day.
    $ns=[Xml.XmlNamespaceManager]::new($Document.NameTable)
    $ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    $triggers=@($Document.SelectNodes('/t:Task/t:Triggers/*',$ns))
    if($triggers.Count -eq 0) { return @{due=@(); fallback=$false} }
    $zone=[TimeZoneInfo]::Local
    $today=[TimeZoneInfo]::ConvertTime($Now,$zone).Date
    $due=@()
    foreach($trigger in $triggers) {
        if($trigger.LocalName -cne 'CalendarTrigger' -or $trigger.SelectSingleNode('t:Repetition',$ns) -or
           $trigger.SelectSingleNode('t:EndBoundary',$ns)) { return @{due=@($Now.UtcDateTime.Date.ToString('yyyyMMddTHHmmssZ')); fallback=$true} }
        $enabled=$trigger.SelectSingleNode('t:Enabled',$ns)
        if($enabled -and $enabled.InnerText -ieq 'false') { continue }
        $startNode=$trigger.SelectSingleNode('t:StartBoundary',$ns)
        $start=[DateTime]::MinValue
        if(-not $startNode -or -not [DateTime]::TryParseExact($startNode.InnerText,'yyyy-MM-ddTHH:mm:ss',
            [Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::None,[ref]$start)) {
            return @{due=@($Now.UtcDateTime.Date.ToString('yyyyMMddTHHmmssZ')); fallback=$true}
        }
        if($start.Date -gt $today) { continue }
        $day=$trigger.SelectSingleNode('t:ScheduleByDay/t:DaysInterval',$ns)
        $week=$trigger.SelectSingleNode('t:ScheduleByWeek',$ns)
        if($day -and -not $week -and $day.InnerText -ceq '1') { $active=$true }
        elseif($week -and -not $day -and $week.SelectSingleNode('t:WeeksInterval',$ns).InnerText -ceq '1') {
            $days=$week.SelectSingleNode('t:DaysOfWeek',$ns)
            if(-not $days) { return @{due=@($Now.UtcDateTime.Date.ToString('yyyyMMddTHHmmssZ')); fallback=$true} }
            $active=$null -ne $days.SelectSingleNode('t:'+$today.DayOfWeek.ToString(),$ns)
        } else { return @{due=@($Now.UtcDateTime.Date.ToString('yyyyMMddTHHmmssZ')); fallback=$true} }
        if(-not $active) { continue }
        $local=[DateTime]::SpecifyKind($today.Add($start.TimeOfDay),[DateTimeKind]::Unspecified)
        if($zone.IsAmbiguousTime($local) -or $zone.IsInvalidTime($local)) {
            return @{due=@($Now.UtcDateTime.Date.ToString('yyyyMMddTHHmmssZ')); fallback=$true}
        }
        $utc=[TimeZoneInfo]::ConvertTimeToUtc($local,$zone)
        if($utc -le $Now.UtcDateTime) { $due+=@($utc.ToString('yyyyMMddTHHmmssZ')) }
    }
    return @{due=@($due | Sort-Object -Unique); fallback=$false}
}

function ConvertFrom-CreWakeAction([string]$Arguments, [string]$Launcher, [string]$LinuxRepo, [hashtable]$Defaults) {
    $argv=[string[]][CreReviewedArgs.Parser]::Parse($Arguments)
    if($argv.Count -lt 4 -or $argv[0] -ine '-NoProfile' -or $argv[1] -ine '-NonInteractive' -or
       $argv[2] -ine '-File' -or [IO.Path]::GetFullPath($argv[3]) -ine [IO.Path]::GetFullPath($Launcher)) { throw 'Task launcher differs' }
    $valueNames=@('Tier','Source','Sources','ExpectedSha','WorkDeadlineSeconds','SourceWorkers','AttemptsPerSource',
        'Concurrency','PageCap','AdmitCurrentInventorySource','AdmitCurrentInventorySources','DaumSubsetPolicy',
        'Distro','LinuxUser','LinuxRepo','LinuxEnvFile','AlertWebhookFile','NotificationMode')
    $switchNames=@('Pipeline','AcknowledgeDatabaseWrites','AdmitDaumVerifiedSubset')
    $values=@{}; $flags=@{}
    for($i=4;$i -lt $argv.Count;$i++) {
        $name=$argv[$i].TrimStart('-')
        if(-not $argv[$i].StartsWith('-') -or $values.ContainsKey($name) -or $flags.ContainsKey($name)) {throw 'Task argument differs'}
        if($name -iin $switchNames) { $flags[$name]=$true }
        elseif($name -iin $valueNames -and ++$i -lt $argv.Count -and -not $argv[$i].StartsWith('-')) { $values[$name]=$argv[$i] }
        else { throw 'Task argument is unsupported' }
    }
    if($values['Tier'] -cne 'series' -or -not $flags.ContainsKey('AcknowledgeDatabaseWrites') -or
       -not $values.ContainsKey('ExpectedSha') -or -not $values.ContainsKey('WorkDeadlineSeconds')) {throw 'Task is not a bound series'}
    $pipeline=$flags.ContainsKey('Pipeline')
    if($values.ContainsKey('Sources') -eq $values.ContainsKey('Source') -or ($values.ContainsKey('Sources') -and -not $pipeline)) {throw 'Task source scope differs'}
    $sourceText=if($values.ContainsKey('Sources')){$values['Sources']}else{$values['Source']}
    $sources=@($sourceText.Split(','))
    $admissions=@()
    if($values.ContainsKey('AdmitCurrentInventorySource')) {$admissions+=@($values['AdmitCurrentInventorySource'])}
    if($values.ContainsKey('AdmitCurrentInventorySources')) {$admissions+=@($values['AdmitCurrentInventorySources'].Split(','))}
    function Pick($name,$default) {if($values.ContainsKey($name)) {return $values[$name]} return $default}
    $daum=$flags.ContainsKey('AdmitDaumVerifiedSubset')
    $policy=if($daum){Pick 'DaumSubsetPolicy' 'tenure-and-empty-title-v2'}else{Pick 'DaumSubsetPolicy' 'tenure-only-v1'}
    $workers=if($pipeline){[int](Pick 'SourceWorkers' 2)}else{1}
    return @{pipeline=$pipeline; sources=$sources; admissions=$admissions; daum_subset=$daum; daum_subset_policy=$policy;
        concurrency=[int](Pick 'Concurrency' $Defaults['Concurrency']); workers=$workers; page_cap=[int](Pick 'PageCap' $Defaults['PageCap']);
        attempts=[int](Pick 'AttemptsPerSource' $Defaults['AttemptsPerSource']); deadline_seconds=[int]$values['WorkDeadlineSeconds'];
        expected_sha=[string]$values['ExpectedSha']; env_file=[string](Pick 'LinuxEnvFile' $Defaults['LinuxEnvFile']);
        windows_launcher=$Launcher; windows_launcher_sha256=(Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLowerInvariant();
        task_path=$null; task_name=$null; task_xml_sha256=$null;
        windows_repo=[string](Pick 'LinuxRepo' $Defaults['LinuxRepo']); distro=[string](Pick 'Distro' $Defaults['Distro']);
        linux_user=[string](Pick 'LinuxUser' $Defaults['LinuxUser']); alert_file=if($values.ContainsKey('AlertWebhookFile')){$values['AlertWebhookFile']}else{$null};
        notification_mode=[string](Pick 'NotificationMode' $Defaults['NotificationMode'])}
}

function Invoke-CreDispatcherWake {
    if($env:OS -ne 'Windows_NT') { throw 'Dispatcher wake requires Windows' }
    $launcher=Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
    $defaults=Get-CreWakeDefaults $launcher
    $now=[DateTimeOffset]::UtcNow; $tasks=@()
    foreach($task in @(Get-ScheduledTask -TaskPath '\Agentic Assets\CRE\' -ErrorAction Stop)) {
        if($task.State -eq 'Disabled') { continue }
        if($task.TaskName -cin @('Dispatcher Snapshot','Dispatcher Drain')) { continue }
        $taskId='t_'+(Get-CreWakeHash ($task.TaskPath+$task.TaskName)).Substring(0,24)
        try {
            $xmlText=Export-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName -ErrorAction Stop
            $xml=[Xml.XmlDocument]::new(); $xml.XmlResolver=$null; $xml.LoadXml($xmlText)
            $ns=[Xml.XmlNamespaceManager]::new($xml.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
            $null=Get-CreTaskContract $xmlText
            $actions=@($xml.SelectNodes('/t:Task/t:Actions/t:Exec',$ns))
            $allActions=@($xml.SelectNodes('/t:Task/t:Actions/*',$ns))
            $principal=@($xml.SelectNodes('/t:Task/t:Principals/t:Principal',$ns))
            if($actions.Count -ne 1 -or $allActions.Count -ne 1 -or $principal.Count -ne 1) { throw 'Task action or principal differs' }
            $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            $user=$principal[0].SelectSingleNode('t:UserId',$ns)
            $logon=$principal[0].SelectSingleNode('t:LogonType',$ns)
            if(-not $user -or $user.InnerText -cne $sid -or -not $logon -or $logon.InnerText -cne 'InteractiveToken' -or
               $principal[0].SelectSingleNode('t:GroupId',$ns)) { throw 'Task principal differs' }
            $command=$actions[0].SelectSingleNode('t:Command',$ns)
            $workdir=$actions[0].SelectSingleNode('t:WorkingDirectory',$ns)
            $repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
            $approved=@((Join-Path $env:LOCALAPPDATA 'Programs/PowerShell/7/pwsh.exe'),
                        (Join-Path $env:ProgramFiles 'PowerShell/7/pwsh.exe'))
            if(-not $command -or -not $workdir -or [IO.Path]::GetFullPath($workdir.InnerText) -ine $repo -or
               @($approved | Where-Object {[IO.Path]::GetFullPath($_) -ieq [IO.Path]::GetFullPath($command.InnerText)}).Count -ne 1) {
                throw 'Task executable or working directory differs'
            }
            $args=$actions[0].SelectSingleNode('t:Arguments',$ns).InnerText
            $policy=ConvertFrom-CreWakeAction $args $launcher ([string]$defaults['LinuxRepo']) $defaults
            if($policy.expected_sha -cne $ExpectedSha) {throw 'Task revision differs from dispatcher'}
            $policy.task_path=[string]$task.TaskPath; $policy.task_name=[string]$task.TaskName
            $policy.task_xml_sha256=Get-CreWakeHash $xmlText
            $schedule=Get-CreWakeDue $xml $now
            if($schedule.fallback) {$taskId+='_current_fallback'}
            $tasks+=@(@{task=$taskId; policy=$policy; due=@($schedule.due)})
        } catch {
            # A bad task is held locally; it does not suppress valid task peers.
            $tasks+=@(@{task=$taskId+'_invalid'; policy=@{error='current_task_unsupported'};
                       due=@($now.UtcDateTime.Date.ToString('yyyyMMddTHHmmssZ'))})
        }
    }
    $snapshot=@{schema='cre-dispatch-current-snapshot/v1'; observed_unix_ms=$now.ToUnixTimeMilliseconds(); tasks=$tasks}
    $repo=[string]$defaults['LinuxRepo']; $distro=[string]$defaults['Distro']; $user=[string]$defaults['LinuxUser']
    $payload=$snapshot | ConvertTo-Json -Depth 20 -Compress
    $payload | & wsl.exe -d $distro -u $user --exec python3 -B ($repo+'/scripts/firecrawl-ops/cre_collector/cre_local_dispatcher.py') --snapshot-only
    if($LASTEXITCODE -ne 0) { throw 'Dispatcher wake failed' }
}

if($MyInvocation.InvocationName -ne '.') {
    if($VerifyTaskPath -or $VerifyTaskName -or $ExpectedTaskSha256) {
        if($DrainOnly) {throw 'Task verification cannot drain'}
        if($VerifyTaskPath -cne '\Agentic Assets\CRE\' -or -not $VerifyTaskName -or
           $VerifyTaskName -match '[\\/\r\n]' -or $ExpectedTaskSha256 -cnotmatch '^[a-f0-9]{64}$') {throw 'Task verification input differs'}
        $task=Get-ScheduledTask -TaskPath $VerifyTaskPath -TaskName $VerifyTaskName -ErrorAction Stop
        if($task.State -eq 'Disabled') {throw 'Task disabled'}
        $xml=Export-ScheduledTask -TaskPath $VerifyTaskPath -TaskName $VerifyTaskName -ErrorAction Stop
        if((Get-CreWakeHash $xml) -cne $ExpectedTaskSha256) {throw 'Task policy changed'}
    } else {
        if(-not $ExpectedSha) {throw 'Exact dispatcher revision required'}
        $launcher=Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
        $defaults=Get-CreWakeDefaults $launcher
        $head=& wsl.exe -d ([string]$defaults['Distro']) -u ([string]$defaults['LinuxUser']) --exec git -C ([string]$defaults['LinuxRepo']) rev-parse HEAD
        if($LASTEXITCODE -ne 0 -or @($head).Count -ne 1 -or $head -cne $ExpectedSha) {throw 'Dispatcher runtime revision differs'}
        $dirty=@(& wsl.exe -d ([string]$defaults['Distro']) -u ([string]$defaults['LinuxUser']) --exec git -C ([string]$defaults['LinuxRepo']) status --porcelain --untracked-files=no)
        if($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) {throw 'Dispatcher runtime has tracked changes'}
        if($DrainOnly) {
            & wsl.exe -d ([string]$defaults['Distro']) -u ([string]$defaults['LinuxUser']) --exec python3 -B `
                ([string]$defaults['LinuxRepo']+'/scripts/firecrawl-ops/cre_collector/cre_local_dispatcher.py') --wake
            if($LASTEXITCODE -ne 0) {throw 'Dispatcher drain failed'}
        } else { Invoke-CreDispatcherWake }
    }
}
