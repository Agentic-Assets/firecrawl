# Bounded read-only Scheduler evidence for the finite Enabled restoration.
function Get-CreRestorationLogTip {
    $channel='Microsoft-Windows-TaskScheduler/Operational'
    $log=Get-WinEvent -ListLog $channel -ErrorAction Stop
    if(-not $log.IsEnabled -or $log.RecordCount -le 0) {throw 'Scheduler history visibility unavailable'}
    return Get-WinEvent -LogName $channel -MaxEvents 1 -ErrorAction Stop
}

function Save-CreRestorationWatermark([string]$Output) {
    $tip=Get-CreRestorationLogTip
    $watermark=[ordered]@{schema='cre-restoration-watermark-v1';channel=$tip.LogName;computer=$tip.MachineName;
        record_id=[long]$tip.RecordId;record_sha256=(Get-CreHandoffHash $tip.ToXml());captured_at=[DateTime]::UtcNow.ToString('o')}
    Save-CreHandoffExclusive (Join-Path $Output 'scheduler-watermark.json') ($watermark|ConvertTo-Json)
}

function Get-CreRestorationExecution($Release,[string]$Origin,[string]$Output,$Tasks) {
    $started=[DateTime]::UtcNow.ToString('o')
    $watermarkPath=Join-Path (ConvertTo-CreHandoffPath $Origin) 'scheduler-watermark.json'
    $watermarkRaw=[IO.File]::ReadAllText($watermarkPath)
    $watermark=Read-CreHandoffJson $watermarkRaw
    $tip=Get-CreRestorationLogTip
    if($tip.LogName -cne $watermark.channel -or $tip.MachineName -cne $watermark.computer -or $tip.RecordId -lt $watermark.record_id) {throw 'Scheduler log identity/watermark changed'}
    $anchor=Get-WinEvent -LogName $watermark.channel -FilterXPath ('*[System[EventRecordID='+$watermark.record_id+']]') -MaxEvents 1 -ErrorAction Stop
    if((Get-CreHandoffHash $anchor.ToXml()) -cne $watermark.record_sha256) {throw 'Scheduler log truncated or replaced'}
    $events=@()
    if($tip.RecordId -gt $watermark.record_id) {
        foreach($row in $Release.task_baseline) {
            $taskPath=[string]$row.task_path+[string]$row.task_name
            if($taskPath.Contains("'")){throw 'Unsupported task path'}
            $query="*[System[EventRecordID > $($watermark.record_id) and EventRecordID <= $($tip.RecordId)] and EventData[Data[@Name='TaskName']='$taskPath']]"
            try {$found=@(Get-WinEvent -LogName $watermark.channel -FilterXPath $query -Oldest -MaxEvents 257 -ErrorAction Stop)}
            catch {if($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*'){throw};$found=@()}
            if($found.Count -gt 256){throw 'Scheduler evidence interval exceeds finite bound'}
            $events+=@($found|ForEach-Object {$_.ToXml()})
        }
    }
    $service=New-Object -ComObject Schedule.Service
    $service.Connect()
    $instances=@()
    $principals=@()
    $bridges=@()
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $observer=[Security.Principal.WindowsPrincipal]::new($identity)
    $observerAdmin=$observer.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    foreach($row in $Release.task_baseline) {
        $registered=$service.GetFolder([string]$row.task_path).GetTask([string]$row.task_name)
        $user=[string]$registered.Definition.Principal.UserId
        $account=if($user -match '^S-1-') {[Security.Principal.SecurityIdentifier]::new($user)}else{[Security.Principal.NTAccount]::new($user)}
        $sid=$account.Translate([Security.Principal.SecurityIdentifier])
        $principals+=@{key=$row.key;user_id=$user;sid=$sid.Value;aliases=@($user,$sid.Value,$sid.Translate([Security.Principal.NTAccount]).Value)}
        if(-not $observerAdmin -and $identity.User.Value -cne $sid.Value) {throw 'Scheduler instance visibility below task principal'}
        foreach($active in $registered.GetInstances(0)) {
            $instances+=@{key=$row.key;instance_guid=[string]$active.InstanceGuid;engine_pid=[int]$active.EnginePID;state=[int]$active.State}
            $related=@($events|ForEach-Object {[xml]$_}|Where-Object {
                $data=@{};foreach($item in $_.Event.EventData.Data){$data[[string]$item.Name]=[string]$item.'#text'}
                ($data['TaskInstanceId'] -ieq $active.InstanceGuid -or $data['InstanceId'] -ieq $active.InstanceGuid)
            })
            $actionEvent=@($related|Where-Object {[int]$_.Event.System.EventID -eq 200})
            if($actionEvent.Count -ne 1){continue}
            $actionData=@{};foreach($item in $actionEvent[0].Event.EventData.Data){$actionData[[string]$item.Name]=[string]$item.'#text'}
            $actionPid=[int]$actionData.EnginePID
            $process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$actionPid) -ErrorAction Stop
            if(-not $process){continue}
            $children=@(Get-CimInstance Win32_Process -Filter ("ParentProcessId="+$actionPid+" AND Name='wsl.exe'") -ErrorAction Stop)
            if($children.Count -ne 1){continue}
            $child=$children[0];$scalars=@{};$valid=$true
            foreach($flag in @('expected-sha','sources','windows-admission-unix-ms')) {
                $pattern='(?:^|\s)"?--'+$flag+'"?\s+"?([A-Za-z0-9,_-]+)"?(?=\s|$)'
                $matches=[regex]::Matches([string]$child.CommandLine,$pattern)
                if($matches.Count -ne 1){$valid=$false;break}
                $scalars[$flag]=$matches[0].Groups[1].Value
            }
            if(-not $valid){continue}
            $ordered=@(foreach($eventId in @(107,129,100,200)) {
                $matched=@($related|Where-Object {[int]$_.Event.System.EventID -eq $eventId})
                if($matched.Count -ne 1){$valid=$false;break};[long]$matched[0].Event.System.EventRecordID
            })
            if(-not $valid){continue}
            $bridges+=@{observed_at=[DateTime]::UtcNow.ToString('o');task_instance_guid=[string]$active.InstanceGuid;
                task_path=([string]$row.task_path+[string]$row.task_name);scheduler_event_record_ids=$ordered;
                action_pid=$actionPid;action_creation_utc=$process.CreationDate.ToUniversalTime().ToString('o');
                direct_wsl_pid=[int]$child.ProcessId;direct_wsl_parent_pid=[int]$child.ParentProcessId;
                direct_wsl_creation_utc=$child.CreationDate.ToUniversalTime().ToString('o');scalar_argv=$scalars}
        }
    }
    $record=[ordered]@{schema='cre-restoration-execution-v1';watermark=$watermark;
        watermark_reference=@{path=$watermarkPath.Replace('\','/');sha256=(Get-CreHandoffHash $watermarkRaw);bytes=([IO.File]::ReadAllBytes($watermarkPath)).Length};
        channel=$tip.LogName;computer=$tip.MachineName;upper_record_id=[long]$tip.RecordId;
        started_at=$started;finished_at=[DateTime]::UtcNow.ToString('o');events=$events;instances=$instances;principals=$principals;bridges=$bridges;
        observer_sid=$identity.User.Value;observer_is_admin=$observerAdmin;
        tasks=$Tasks;scope='bounded Scheduler history only; source settlement unverified; delayed catch-up remains possible'}
    $path=Join-Path $Output 'scheduler-observation.json'
    Save-CreHandoffExclusive $path ($record|ConvertTo-Json -Depth 18)
    return @{path=$path.Replace('\','/');sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant();bytes=([IO.File]::ReadAllBytes($path)).Length}
}
