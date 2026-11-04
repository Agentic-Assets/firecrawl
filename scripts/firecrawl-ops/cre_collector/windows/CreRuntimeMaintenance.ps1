# Inert native operations for a reviewed runtime update. This path never enables jobs.
function Get-CreMaintenanceTaskInstances($Release) {
    $service=New-Object -ComObject Schedule.Service
    $service.Connect()
    foreach($row in $Release.task_baseline) {
        $registered=Get-CreRestorationRegisteredTask $service $row
        $count=$registered.GetInstances(0).Count
        if($count -gt 0) { @{key=$row.key;count=[int]$count} }
    }
}

function Assert-CreMaintenanceIdle($Release) {
    Assert-CreHandoffFleet $Release
    if(@(Get-CreHandoffNativeFrontends).Count) {throw 'Visible competing native CRE frontend exists'}
    if(@(Get-CreMaintenanceTaskInstances $Release).Count) {throw 'Scheduled instance exists during maintenance'}
}

function Get-CreMaintenanceBeforePin($Release,$Row) {
    $pin=$Release.old.sha
    if($Row -is [System.Collections.IDictionary]) {
        if($Row.Contains('pin_before')) {$pin=$Row['pin_before']}
    } elseif($null -ne $Row.PSObject.Properties['pin_before']) {
        $pin=$Row.pin_before
    }
    if($pin -isnot [string] -or $pin -cnotmatch '^[0-9a-f]{40}$') {throw 'Invalid maintenance before pin'}
    return $pin
}

function Get-CreMaintenanceTask($Release,$Row,[bool]$Selected) {
    if($Row.enabled -isnot [bool] -or $Row.enabled) {throw 'Maintenance baseline must be held'}
    $xml=Read-CreHandoffBaselineXml $Release $Row
    if($Selected) {$xml=Pin-ReviewedXml $xml (Get-CreMaintenanceBeforePin $Release $Row) $Release.new.sha}
    $record=Get-CreHandoffTask $Row $xml $true $true
    if($record.enabled -or $record.state -cne 'Disabled') {throw 'Maintenance requires exact held fleet'}
    return $record
}

function Invoke-CreRuntimeMaintenanceTasks($Release,[string]$Mode,[string]$Output,[scriptblock]$AssertOwner,$Operation=$null,[string]$OperationOutput='') {
    if($Mode -notin @('Observe','Hold','Repin','Verify','Postreadback')) {throw 'Unsupported maintenance task mode'}
    $rows=@($Release.task_baseline)
    if($rows.Count -ne 3 -or @($rows.key|Select-Object -Unique).Count -ne 3) {throw 'Duplicate or missing baseline task'}
    Assert-CreMaintenanceIdle $Release
    $selected=$Mode -in @('Verify','Postreadback')
    if($Mode -in @('Hold','Repin','Verify')) {& $AssertOwner}
    $admission=Get-CreHandoffAdmission $Release $Output $Mode.ToLowerInvariant()
    if(-not $admission.admitted) {throw 'Windows resource admission refused'}
    # Check the complete fleet before the first registration or mutation marker.
    $tasks=@(foreach($row in $rows) {Get-CreMaintenanceTask $Release $row $selected})
    if($Mode -eq 'Hold') {
        if(-not $Operation -or $Operation.schema -cne 'cre-runtime-maintenance-mutation-v1') {throw 'Maintenance mutation context missing'}
        foreach($record in $tasks) {Save-CreHandoffExclusive (Join-Path $Output ($record.key+'-before.xml')) $record.xml}
        Save-CreRestorationWatermark $Output
        & $AssertOwner
        Assert-CreMaintenanceIdle $Release
        $Operation['entered_at']=[DateTime]::UtcNow.ToString('o')
        Save-CreHandoffExclusive (Join-Path $Output 'mutation-start.json') ($Operation|ConvertTo-Json -Depth 15)
        return
    }
    if($Mode -eq 'Repin') {
        foreach($row in $rows) {
            & $AssertOwner
            Assert-CreMaintenanceIdle $Release
            $null=Get-CreMaintenanceTask $Release $row $false
            $wanted=Pin-ReviewedXml (Read-CreHandoffBaselineXml $Release $row) (Get-CreMaintenanceBeforePin $Release $row) $Release.new.sha
            $null=Register-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -Xml $wanted -Force -ErrorAction Stop
            $record=Get-CreMaintenanceTask $Release $row $true
            Save-CreHandoffExclusive (Join-Path $Output ($row.key+'-final.xml')) $record.xml
        }
        Assert-CreMaintenanceIdle $Release
        return
    }
    foreach($record in $tasks) {
        $xmlPath=Join-Path $Output ($record.key+'-observed.xml')
        Save-CreHandoffExclusive $xmlPath $record.xml
        $record['xml_reference']=@{path=(ConvertTo-CreHandoffPath $xmlPath);sha256=$record.xml_sha256;
            bytes=([Text.UTF8Encoding]::new($false).GetByteCount($record.xml))}
        $record.Remove('xml')
    }
    $execution=$null
    if($Mode -in @('Verify','Postreadback')) {
        $origin=if($Mode -eq 'Verify'){$Output}else{$OperationOutput}
        if(-not $origin) {throw 'Original maintenance output required for Scheduler observation'}
        $execution=Get-CreRestorationExecution $Release $origin $Output $tasks
    }
    return [ordered]@{observed_at=[DateTime]::UtcNow.ToString('o');tasks=$tasks;windows_resources=$admission;
        native_frontends=@();native_visibility='pwsh/powershell and exact Scheduler instances; own bounded ancestry excluded';execution=$execution}
}
