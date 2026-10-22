# Inert native fleet operations. Importing this file makes no host calls.
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'CreTaskContract.ps1')

function ConvertTo-CreHandoffPath([string]$Path) {
    $normalized=$Path.Replace('\','/')
    if($normalized -cmatch '^/mnt/([a-z])/(.+)$') {
        $normalized=$Matches[1].ToUpperInvariant()+':/'+$Matches[2]
    }
    if($normalized -cnotmatch '^[A-Z]:/' -or $normalized -match '/[.][.]?(/|$)') { throw 'Absolute canonical Windows path required' }
    $full=[IO.Path]::GetFullPath($normalized).Replace('\','/')
    if($full -cne $normalized) { throw 'Path normalization changed identity beyond separators' }
    $current=$full
    while($current) {
        if(Test-Path -LiteralPath $current) {
            $item=Get-Item -LiteralPath $current -Force
            if($item.Attributes -band [IO.FileAttributes]::ReparsePoint -or $item.LinkType -ceq 'HardLink') { throw 'Handoff input/root alias' }
            $leaf=Split-Path -Leaf $current.TrimEnd('/')
            if($leaf -and $leaf -notmatch '^[A-Z]:$' -and $item.Name -cne $leaf) { throw 'Handoff input/root case drift' }
        }
        $parent=Split-Path -Parent $current
        if($parent -eq $current) { break }; $current=$parent
    }
    return $full
}

function Save-CreHandoffExclusive([string]$Path,[string]$Text) {
    $stream=[IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try { $raw=[Text.UTF8Encoding]::new($false).GetBytes($Text);$stream.Write($raw,0,$raw.Length);$stream.Flush($true) }
    finally { $stream.Dispose() }
}

function Get-CreHandoffHash([string]$Text) {
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash([Text.UTF8Encoding]::new($false).GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Pin-ReviewedXml([string]$Xml,[string]$OldSha,[string]$NewSha) {
    if($OldSha -cnotmatch '^[0-9a-f]{40}$' -or $NewSha -cnotmatch '^[0-9a-f]{40}$' -or $OldSha -ceq $NewSha) { throw 'Invalid exact code pins' }
    $doc=[Xml.XmlDocument]::new();$doc.XmlResolver=$null;$doc.LoadXml($Xml)
    $ns=[Xml.XmlNamespaceManager]::new($doc.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    $arguments=$doc.SelectNodes('/t:Task/t:Actions/t:Exec/t:Arguments',$ns)
    if($doc.SelectNodes('/t:Task/t:Actions/*',$ns).Count -ne 1 -or $arguments.Count -ne 1) { throw 'Unexpected task action shape' }
    $parameter='(?<!\S)(?:"-ExpectedSha"|-ExpectedSha)(?=\s|$)'
    $binding=$parameter+'\s+(?:"'+$OldSha+'"|'+$OldSha+')(?=\s|$)'
    if([regex]::Matches($arguments[0].InnerText,$parameter).Count -ne 1 -or
       [regex]::Matches($arguments[0].InnerText,$binding).Count -ne 1 -or
       [regex]::Matches($Xml,[regex]::Escape($OldSha)).Count -ne 2) { throw 'Expected exact action and description pins' }
    return $Xml.Replace($OldSha,$NewSha)
}

function Set-CreHandoffEnabledXml([string]$Xml,[bool]$Enabled) {
    $doc=[Xml.XmlDocument]::new();$doc.XmlResolver=$null;$doc.LoadXml($Xml)
    $ns=[Xml.XmlNamespaceManager]::new($doc.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    $parent=$doc.SelectSingleNode('/t:Task/t:Settings',$ns)
    if(-not $parent) { throw 'Task settings missing' }
    $node=$parent.SelectSingleNode('t:Enabled',$ns)
    if(-not $node) { $node=$doc.CreateElement('Enabled',$ns.LookupNamespace('t'));[void]$parent.AppendChild($node) }
    $node.InnerText=$Enabled.ToString().ToLowerInvariant(); return $doc.OuterXml
}

function Assert-CreHandoffFleet($Release) {
    $expected=@('\Agentic Assets\CRE\Interra Refresh','\Agentic Assets\CRE\Newmark Refresh','\Agentic Assets\CRE\Qualified Brokerage Pipeline')
    $relevant=@(foreach($task in @(Get-ScheduledTask -ErrorAction Stop)) {
        $matches=$task.TaskPath -ceq '\Agentic Assets\CRE\'
        foreach($action in @($task.Actions)) {
            foreach($field in @('Execute','Arguments','WorkingDirectory')) {
                $property=$action.PSObject.Properties[$field]
                if($null -eq $property) { continue }
                $wire=([string]$property.Value).Replace('\','/')
                foreach($root in @($Release.roots.native,$Release.roots.runtime)) {
                    if($wire.IndexOf(([string]$root).Replace('\','/'),[StringComparison]::OrdinalIgnoreCase) -ge 0) { $matches=$true }
                }
                if($wire -match 'Invoke-CreGuardedTier') { $matches=$true }
            }
        }
        if($matches) { $task.TaskPath+$task.TaskName }
    })
    if($relevant.Count -ne 3 -or @($relevant|Select-Object -Unique).Count -ne 3) { throw 'Whole CRE fleet count/uniqueness differs' }
    foreach($name in $expected) { if(@($relevant|Where-Object {$_ -ceq $name}).Count -ne 1) { throw 'Unexpected or missing CRE task' } }
}

function Get-CreHandoffTask($Baseline,[string]$Xml,[bool]$RequireNext=$true) {
    $task=Get-ScheduledTask -TaskPath $Baseline.task_path -TaskName $Baseline.task_name -ErrorAction Stop
    $info=Get-ScheduledTaskInfo -TaskPath $Baseline.task_path -TaskName $Baseline.task_name -ErrorAction Stop
    if($task.Settings.Enabled -isnot [bool]) { throw 'Enabled must be Boolean' }
    $live=Export-ScheduledTask -TaskPath $Baseline.task_path -TaskName $Baseline.task_name -ErrorAction Stop
    if((Get-CreTaskContract $live) -cne (Get-CreTaskContract $Xml)) { throw 'Complete task definition changed' }
    $last=$info.LastRunTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $next=$info.NextRunTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    if([int]$info.LastTaskResult -ne $Baseline.last_task_result -or $last -cne $Baseline.last_run_utc -or ($RequireNext -and $next -cne $Baseline.next_run_utc)) { throw 'Task history/result/clocks changed' }
    return [ordered]@{key=$Baseline.key;task_path=[string]$task.TaskPath;task_name=[string]$task.TaskName;
        enabled=$task.Settings.Enabled;state=[string]$task.State;last_task_result=[int]$info.LastTaskResult;
        last_run_utc=$last;next_run_utc=$next;xml_sha256=(Get-CreHandoffHash $live);xml=$live;
        actions=@($task.Actions|ForEach-Object {[ordered]@{execute=[string]$_.Execute;arguments=[string]$_.Arguments;working_directory=[string]$_.WorkingDirectory}})}
}

function Read-CreHandoffBaselineXml($Release,$Baseline) {
    $pins=@($Release.inputs|Where-Object {$_.role -ceq $Baseline.xml})
    if($pins.Count -ne 1) { throw 'Expected one baseline XML pin' }
    $path=ConvertTo-CreHandoffPath $pins[0].path
    $raw=[IO.File]::ReadAllBytes($path)
    $text=[Text.UTF8Encoding]::new($false,$true).GetString($raw)
    if((Get-CreHandoffHash $text) -cne $pins[0].sha256 -or $raw.Length -ne $pins[0].bytes) { throw 'Baseline XML bytes changed' }
    return $text
}

function Get-CreHandoffAdmission($Release,[string]$Output,[string]$Stage='observe') {
    if(-not ('CreWindowsCapture.Capture' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'CreNativeCapture.cs') }
    $stdout=Join-Path $Output ($Stage+'-windows-admission.stdout.raw')
    $stderr=Join-Path $Output ($Stage+'-windows-admission.stderr.raw')
    $result=[CreWindowsCapture.Capture]::RunReadOnly((ConvertTo-CreHandoffPath $Release.programs.powershell),
        ([string[]]@('-NoProfile','-NonInteractive','-File',(Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'),'-CheckOnly','-ExpectedSha',$Release.deployed.sha)),
        $stdout,$stderr,15000)
    Save-CreHandoffExclusive (Join-Path $Output ($Stage+'-windows-admission.capture.json')) ($result|ConvertTo-Json -Depth 8)
    if($result.Failed -or $result.NativeExitCode -ne 0 -or -not $result.Stdout.Eof -or -not $result.Stderr.Eof -or $result.Stdout.Truncated -or $result.Stderr.Truncated) { throw 'Windows CheckOnly resource observation unsettled/refused' }
    $admission=[IO.File]::ReadAllText($stdout)|ConvertFrom-Json
    if($admission.admitted -isnot [bool] -or $admission.admitted -ne $true -or
       $admission.cpuPercent -is [bool] -or $admission.availablePhysicalGiB -is [bool] -or
       [double]::IsNaN([double]$admission.cpuPercent) -or [double]::IsInfinity([double]$admission.cpuPercent) -or
       [double]::IsNaN([double]$admission.availablePhysicalGiB) -or [double]::IsInfinity([double]$admission.availablePhysicalGiB) -or
       $admission.cpuPercent -lt 0 -or $admission.cpuPercent -ge 90 -or $admission.availablePhysicalGiB -lt 8) { throw 'Windows resource admission refused' }
    return $admission
}

function Get-CreHandoffNativeFrontends {
    # Inspect only the two native PowerShell hosts. Never return command lines.
    $ancestors=[Collections.Generic.HashSet[int]]::new();$cursor=$PID
    for($index=0;$index -lt 12 -and $cursor -gt 0;$index++) {
        if(-not $ancestors.Add([int]$cursor)) { throw 'Native ancestry cycle' }
        $process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$cursor) -ErrorAction Stop
        if(-not $process) { break };$cursor=[int]$process.ParentProcessId
    }
    if($cursor -gt 0 -and $index -ge 12) { throw 'Native ancestry visibility bound exceeded' }
    return @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' OR Name='powershell.exe'" -ErrorAction Stop |
        Where-Object {
            -not $ancestors.Contains([int]$_.ProcessId) -and
            ($_.CommandLine -like '*Invoke-CreGuardedTier.ps1*' -or $_.CommandLine -like '*Invoke-CreCodeHandoff.ps1*' -or
             $_.CommandLine -like '*deploy-windows.ps1*' -or $_.CommandLine -like '*finalize-tasks.ps1*')
        } | ForEach-Object {[ordered]@{pid=[int]$_.ProcessId;name=[string]$_.Name}})
}

function Invoke-CreHandoffTasks($Release,[string]$Mode,[string]$Output,[scriptblock]$AssertOwner,$Operation=$null) {
    Assert-CreHandoffFleet $Release
    $rows=@($Release.task_baseline)
    if($rows.Count -ne 3 -or @($rows.key|Select-Object -Unique).Count -ne 3) { throw 'Duplicate or missing baseline task' }
    if($Mode -in @('Observe','Postreadback','Verify')) {
        $admission=Get-CreHandoffAdmission $Release $Output $Mode.ToLowerInvariant()
        if(-not $admission.admitted) { throw 'Windows resource admission refused' }
        $frontends=@(Get-CreHandoffNativeFrontends)
        if($frontends.Count) { throw 'Visible competing native CRE frontend exists' }
        $tasks=@(foreach($row in $rows) {
            $xml=Read-CreHandoffBaselineXml $Release $row
            if($Mode -ne 'Observe') { $xml=Pin-ReviewedXml $xml $Release.deployed.sha $Release.selected.sha }
            $record=Get-CreHandoffTask $row $xml
            Save-CreHandoffExclusive (Join-Path $Output ($row.key+'-observed.xml')) $record.xml
            $record.Remove('xml'); $record
        })
        return [ordered]@{observed_at=[DateTime]::UtcNow.ToString('o');tasks=$tasks;windows_resources=$admission;
            native_frontends=$frontends;native_visibility='pwsh/powershell only; own bounded ancestry excluded; no command lines retained'}
    }
    & $AssertOwner
    $admission=Get-CreHandoffAdmission $Release $Output $Mode.ToLowerInvariant()
    Save-CreHandoffExclusive (Join-Path $Output ($Mode.ToLowerInvariant()+'-windows-resources.json')) ($admission|ConvertTo-Json -Depth 8)
    if(@(Get-CreHandoffNativeFrontends).Count) { throw 'Competing native CRE frontend at mutation boundary' }
    & {
        # Validate every member before changing any member.
        foreach($row in $rows) {
            $xml=Read-CreHandoffBaselineXml $Release $row
            if($Mode -in @('Repin','Restore')) { $xml=Set-CreHandoffEnabledXml $xml $false }
            if($Mode -eq 'Restore') { $xml=Pin-ReviewedXml $xml $Release.deployed.sha $Release.selected.sha }
            $observed=Get-CreHandoffTask $row $xml ($Mode -eq 'Hold')
            if($Mode -eq 'Hold' -and $observed.xml -cne $xml) { throw 'Fresh admission complete XML bytes changed' }
        }
        if($Mode -ne 'Restore') { foreach($row in $rows) {
            & $AssertOwner
            $before=Read-CreHandoffBaselineXml $Release $row
            if($Mode -eq 'Hold') {
                Save-CreHandoffExclusive (Join-Path $Output ($row.key+'-before.xml')) $before
                if(-not (Test-Path -LiteralPath (Join-Path $Output 'mutation-start.json'))) {
                    if(-not $Operation) { throw 'Mutation context missing' }
                    Save-CreHandoffExclusive (Join-Path $Output 'mutation-start.json') ($Operation|ConvertTo-Json -Depth 15)
                }
                $null=Disable-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop
                $wanted=Set-CreHandoffEnabledXml $before $false
            } elseif($Mode -eq 'Repin') {
                $wanted=Set-CreHandoffEnabledXml (Pin-ReviewedXml $before $Release.deployed.sha $Release.selected.sha) $false
                $null=Register-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -Xml $wanted -Force -ErrorAction Stop
            } else { throw 'Unknown task mutation mode' }
            $record=Get-CreHandoffTask $row $wanted $false
            if($record.state -cne 'Disabled' -or $record.enabled) { throw 'Held task state uncertain' }
            Save-CreHandoffExclusive (Join-Path $Output ($row.key+'-'+$Mode+'.xml')) $record.xml
        } }
        if($Mode -eq 'Restore') {
            Assert-CreHandoffFleet $Release
            & $AssertOwner
            foreach($row in $rows) {
                if($row.enabled -and ([DateTimeOffset]::Parse($row.next_run_utc)-[DateTimeOffset]::UtcNow).TotalSeconds -lt 900) { throw 'Next trigger too close for restoration' }
            }
            foreach($row in $rows) {
                & $AssertOwner
                if($row.enabled) { $null=Enable-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop }
                $wanted=Set-CreHandoffEnabledXml (Pin-ReviewedXml (Read-CreHandoffBaselineXml $Release $row) $Release.deployed.sha $Release.selected.sha) ([bool]$row.enabled)
                $record=Get-CreHandoffTask $row $wanted
                if($record.enabled -ne $row.enabled -or $record.state -cne $(if($row.enabled){'Ready'}else{'Disabled'})) { throw 'Restoration state uncertain' }
                Save-CreHandoffExclusive (Join-Path $Output ($row.key+'-final.xml')) $record.xml
            }
        }
        Assert-CreHandoffFleet $Release
    }
}

function Invoke-CreHandoffFailureHold($Release,[string]$Output,[string]$ReleaseSha,[string]$Authorization,[string]$ReceiptName='failure-hold.json') {
    # This cleanup never admits a new operation or stops a running owner. It is
    # valid only after this exact operation durably entered its first mutation.
    $markerPath=Join-Path $Output 'mutation-start.json'
    if(-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { return @{attempted=$false;reason='no-mutation-start'} }
    $marker=[IO.File]::ReadAllText((ConvertTo-CreHandoffPath $markerPath))|ConvertFrom-Json
    $authPath=ConvertTo-CreHandoffPath $Authorization
    if($marker.schema -cne 'cre-code-mutation-v1' -or $marker.release_sha256 -cne $ReleaseSha -or
       $marker.authorization_sha256 -cne (Get-CreHandoffHash ([IO.File]::ReadAllText($authPath))) -or
       $marker.operation_output -cne (ConvertTo-CreHandoffPath $Output) -or
       $marker.tooling.sha -cne $Release.tooling.sha -or $marker.tooling.tree -cne $Release.tooling.tree) { throw 'Failure hold operation/source binding differs' }
    $rows=@($Release.task_baseline)
    if($rows.Count -ne 3 -or @($rows.key|Select-Object -Unique).Count -ne 3 -or
       ($marker.fleet|ConvertTo-Json -Depth 15 -Compress) -cne ($rows|ConvertTo-Json -Depth 15 -Compress)) { throw 'Failure hold inventory differs' }
    # Pin every original XML before addressing any task; current pins/expiry are
    # deliberately irrelevant. Each inventoried disable has an independent try.
    foreach($row in $rows) { $null=Read-CreHandoffBaselineXml $Release $row }
    $attempts=@(foreach($row in $rows) {
        $record=[ordered]@{key=$row.key;task_path=$row.task_path;task_name=$row.task_name;disable_attempted=$false;disabled=$false;state=$null;error=$null}
        try {
            $record.disable_attempted=$true
            $null=Disable-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop
            $task=Get-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop
            $record.state=[string]$task.State
            $record.disabled=$task.Settings.Enabled -is [bool] -and -not $task.Settings.Enabled
            if(-not $record.disabled) { $record.error='disable readback uncertain' }
        } catch { $record.error=$_.Exception.GetType().FullName }
        $record
    })
    $frontends=$null;$visibilityError=$null
    try {$frontends=@(Get-CreHandoffNativeFrontends)} catch {$visibilityError=$_.Exception.GetType().FullName}
    $receipt=[ordered]@{schema='cre-code-failure-hold-v1';attempted=$true;release_sha256=$ReleaseSha;tasks=$attempts;does_not_stop_running_owner=$true;
        concurrent_native_frontends=$frontends;visibility_error=$visibilityError;future_hold_stability='not asserted while another frontend/owner may remain';
        recovery_state='not asserted; inspect canonical authority';observed_at=[DateTime]::UtcNow.ToString('o')}
    Save-CreHandoffExclusive (Join-Path $Output $ReceiptName) ($receipt|ConvertTo-Json -Depth 15)
    return $receipt
}

function Assert-CreHandoffTooling($Release,[string]$ActualRoot) {
    $actual=ConvertTo-CreHandoffPath $ActualRoot
    $declared=ConvertTo-CreHandoffPath $Release.roots.tooling
    if($actual -cne $declared) { throw 'Invoked native source root differs from declared tooling' }
    $git=ConvertTo-CreHandoffPath $Release.programs.native_git
    $head=& $git --no-optional-locks -C $actual rev-parse HEAD
    if($LASTEXITCODE -ne 0 -or $head -cne $Release.tooling.sha) { throw 'Actual native tooling SHA differs' }
    $tree=& $git --no-optional-locks -C $actual rev-parse 'HEAD^{tree}'
    if($LASTEXITCODE -ne 0 -or $tree -cne $Release.tooling.tree) { throw 'Actual native tooling tree differs' }
    $status=@(& $git --no-optional-locks -C $actual status --porcelain=v1 --untracked-files=all)
    if($LASTEXITCODE -ne 0 -or $status.Count) { throw 'Actual native tooling tree is not clean' }
}
