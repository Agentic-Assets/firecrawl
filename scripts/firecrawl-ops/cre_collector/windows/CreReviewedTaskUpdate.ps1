# Current-state task operations. Importing this file does not contact Scheduler.
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
. (Join-Path $PSScriptRoot 'CreRestorationExecution.ps1')
. (Join-Path $PSScriptRoot 'CreRuntimeMaintenance.ps1')

# Parse Windows command lines with the same rules used by the installer.
if(-not ('CreReviewedArgs.Parser' -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace CreReviewedArgs {
 public static class Parser {
  [DllImport("shell32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  private static extern IntPtr CommandLineToArgvW(string command, out int count);
  [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
  public static string[] Parse(string value) {
   int count; IntPtr ptr=CommandLineToArgvW("fixture.exe " + value, out count);
   if(ptr==IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
   try { var result=new string[count-1]; for(int i=1;i<count;i++) result[i-1]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(ptr,i*IntPtr.Size)); return result; }
   finally {LocalFree(ptr);}
  }
 }
}
"@
}



function Get-CreReviewedControlHash([string]$Path) {
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try {return ([BitConverter]::ToString($algorithm.ComputeHash([IO.File]::ReadAllBytes($Path)))).Replace('-','').ToLowerInvariant()}
    finally {$algorithm.Dispose()}
}

function Assert-CreReviewedOwner($Release,[string]$ControlPath,[string]$ControlSha,[int]$OwnerPid,[string]$Generation) {
    if($OwnerPid -le 0 -or [string]::IsNullOrWhiteSpace($Generation) -or $ControlSha -cnotmatch '^[0-9a-f]{64}$') {throw 'Exact update owner required'}
    $native=ConvertTo-CreHandoffPath $ControlPath
    if([IO.Path]::GetFileName($native) -cne 'cre_reviewed_update.py' -or
       -not (Test-Path -LiteralPath $native -PathType Leaf) -or
       (Get-CreReviewedControlHash $native) -cne $ControlSha) {throw 'Update control code changed'}
    $linux='/mnt/'+$native.Substring(0,1).ToLowerInvariant()+'/'+$native.Substring(3)
    & wsl.exe -d ([string]$Release.wsl.distro) -u ([string]$Release.wsl.user) --exec python3 -B $linux --owner-check ([string]$Release.roots.runtime) $OwnerPid $Generation
    $code=$LASTEXITCODE
    if((Get-CreReviewedControlHash $native) -cne $ControlSha -or $code -ne 0) {throw 'Update owner or control code changed'}
}

function Get-CreReviewedArgument([string[]]$Arguments,[string]$Name) {
    $indices=@(for($i=0;$i -lt $Arguments.Count;$i++) {if($Arguments[$i] -ieq $Name){$i}})
    if($indices.Count -ne 1 -or $indices[0]+1 -ge $Arguments.Count) {throw 'Missing or duplicate reviewed task argument'}
    return $Arguments[$indices[0]+1]
}

function Assert-CreReviewedSeriesArguments($Release,[string[]]$Argv,[string]$Launcher) {
    $tokens=$null;$errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($Launcher,[ref]$tokens,[ref]$errors)
    if(@($errors).Count -ne 0 -or $null -eq $ast.ParamBlock) {throw 'Guarded launcher parameter metadata unavailable'}
    $known=@{}
    foreach($parameter in @($ast.ParamBlock.Parameters)) {
        $name=[string]$parameter.Name.VariablePath.UserPath
        if(-not $name -or $known.ContainsKey($name)) {throw 'Guarded launcher parameter metadata invalid'}
        $known[$name]=$parameter
    }
    $seen=@{};$parameters=@{}
    for($i=4;$i -lt $Argv.Count;$i++) {
        $key=$Argv[$i]
        if(-not $key.StartsWith('-') -or $key.Length -lt 2 -or -not $known.ContainsKey($key.Substring(1)) -or $key -ieq '-CheckOnly') {throw 'Unsupported series task argument'}
        if($seen.ContainsKey($key)) {throw 'Duplicate series task argument'}
        $seen[$key]=$true
        if($known[$key.Substring(1)].StaticType -ne [Management.Automation.SwitchParameter]) {
            $i++
            if($i -ge $Argv.Count -or [string]::IsNullOrWhiteSpace($Argv[$i]) -or $Argv[$i].StartsWith('-')) {throw 'Series task argument value missing'}
            $parameters[$key]=$Argv[$i]
        }
    }
    $tier=Get-CreReviewedArgument $Argv '-Tier'
    if($tier -ceq 'enrich') {
        $allowed=@('-Tier','-Source','-ExpectedSha','-Batch','-DeadlineSeconds','-Apply','-Distro','-LinuxUser','-LinuxRepo','-LinuxEnvFile','-AlertWebhookFile','-NotificationMode')
        foreach($key in $seen.Keys) {if($key -inotIn $allowed) {throw 'Unsupported enrichment task argument'}}
        if(-not $parameters.ContainsKey('-Batch') -or -not $parameters.ContainsKey('-DeadlineSeconds')) {throw 'Enrichment task batch or deadline missing'}
        $batch=0;$incrementalDeadline=0
        if(-not [int]::TryParse($parameters['-Batch'],[ref]$batch) -or $batch -lt 1 -or $batch -gt 50 -or
           -not [int]::TryParse($parameters['-DeadlineSeconds'],[ref]$incrementalDeadline) -or $incrementalDeadline -lt 30 -or $incrementalDeadline -gt 14400) {throw 'Enrichment task bounds invalid'}
        if($parameters['-Source'] -ceq 'matthews' -and $seen.ContainsKey('-Apply') -and $incrementalDeadline -gt 600) {throw 'Matthews enrichment deadline invalid'}
    } elseif($tier -cne 'series' -or -not $seen.ContainsKey('-AcknowledgeDatabaseWrites') -or -not $parameters.ContainsKey('-WorkDeadlineSeconds')) {throw 'Series task acknowledgement or deadline missing'}
    $deadline=0
    if($tier -ceq 'series' -and (-not [int]::TryParse($parameters['-WorkDeadlineSeconds'],[ref]$deadline) -or $deadline -le 0)) {throw 'Series task deadline invalid'}
    $attempts=0
    if($parameters.ContainsKey('-AttemptsPerSource') -and (-not [int]::TryParse($parameters['-AttemptsPerSource'],[ref]$attempts) -or $attempts -le 0)) {throw 'Series task attempt count invalid'}
    $ranges=@($known['WorkDeadlineSeconds'].Attributes|Where-Object {$_ -is [Management.Automation.Language.AttributeAst] -and $_.TypeName.Name -ieq 'ValidateRange'})
    if($ranges.Count -gt 1) {throw 'Guarded launcher deadline metadata invalid'}
    if($ranges.Count -eq 1 -and $tier -ceq 'series') {
        if($ranges[0].PositionalArguments.Count -ne 2) {throw 'Guarded launcher deadline bounds unavailable'}
        try {$minimum=[int]$ranges[0].PositionalArguments[0].SafeGetValue();$maximum=[int]$ranges[0].PositionalArguments[1].SafeGetValue()} catch {throw 'Guarded launcher deadline bounds are not constant'}
        if($deadline -lt $minimum -or $deadline -gt $maximum) {throw 'Series task deadline invalid'}
    }
    $checks=@(
        @('-Distro',[string]$Release.wsl.distro),
        @('-LinuxUser',[string]$Release.wsl.user),
        @('-LinuxRepo',[string]$Release.roots.runtime),
        @('-LinuxEnvFile',[string]$Release.wsl.env_file)
    )
    foreach($check in $checks) {
        if($parameters.ContainsKey($check[0])) {$actual=[string]$parameters[$check[0]]}
        else {
            $parameter=$known[$check[0].Substring(1)]
            if($null -eq $parameter -or $null -eq $parameter.DefaultValue) {throw 'Guarded launcher target default unavailable'}
            try {$actual=[string]$parameter.DefaultValue.SafeGetValue()} catch {throw 'Guarded launcher target default is not constant'}
        }
        if([string]::IsNullOrWhiteSpace($check[1]) -or $actual -cne $check[1]) {throw 'Series task WSL target differs'}
    }
    return @{known=$known;seen=$seen;parameters=$parameters}
}

function Assert-CreReviewedSelectedBinding($Current,$Selected,[string]$Launcher) {
    $metadata=(Get-Command -Name $Launcher -CommandType ExternalScript -ErrorAction Stop).Parameters
    foreach($name in $Selected.known.Keys) {
        $required=@($metadata[$name].ParameterSets.Values|Where-Object {$_.IsMandatory}).Count -gt 0
        if($required -and -not $Selected.seen.ContainsKey('-'+$name)) {throw "Selected launcher requires -$name"}
    }
    foreach($key in $Selected.seen.Keys) {
        $name=$key.Substring(1);$selectedParam=$Selected.known[$name];$currentParam=$Current.known[$name]
        if($null -eq $currentParam -or $selectedParam.StaticType -ne $currentParam.StaticType) {throw "Selected launcher -$name type requires review"}
        $currentBinding=@($currentParam.Attributes|Where-Object {$_ -is [Management.Automation.Language.AttributeAst] -and $_.TypeName.Name -imatch '^Parameter(Attribute)?$'})
        $selectedBinding=@($selectedParam.Attributes|Where-Object {$_ -is [Management.Automation.Language.AttributeAst] -and $_.TypeName.Name -imatch '^Parameter(Attribute)?$'})
        if($currentBinding.Count -ne $selectedBinding.Count) {throw "Selected launcher -$name parameter binding requires review"}
        for($i=0;$i -lt $currentBinding.Count;$i++) {
            if($currentBinding[$i].Extent.Text -cne $selectedBinding[$i].Extent.Text) {throw "Selected launcher -$name parameter binding requires review"}
        }
        if($selectedParam.StaticType -eq [Management.Automation.SwitchParameter]) {
            $currentAttributes=@($currentParam.Attributes|Where-Object {$_ -is [Management.Automation.Language.AttributeAst]})
            foreach($attribute in @($selectedParam.Attributes|Where-Object {$_ -is [Management.Automation.Language.AttributeAst]})) {
                if($attribute.TypeName.Name -imatch '^Parameter(Attribute)?$') {continue}
                if(@($currentAttributes|Where-Object {$_.Extent.Text -ceq $attribute.Extent.Text}).Count -eq 0) {throw "Selected launcher -$name validation requires review"}
            }
            continue
        }
        if($selectedParam.StaticType -notin @([string],[int],[long],[double],[bool])) {throw "Selected launcher -$name type requires review"}
        try {$value=[Management.Automation.LanguagePrimitives]::ConvertTo($Selected.parameters[$key],$selectedParam.StaticType,[Globalization.CultureInfo]::InvariantCulture)}
        catch {throw "Selected launcher -$name value is invalid"}
        $currentAttributes=@($currentParam.Attributes|Where-Object {$_ -is [Management.Automation.Language.AttributeAst]})
        foreach($attribute in @($selectedParam.Attributes|Where-Object {$_ -is [Management.Automation.Language.AttributeAst]})) {
            $kind=$attribute.TypeName.Name
            switch -Regex ($kind) {
                '^ValidateRange(Attribute)?$' {
                    if($attribute.PositionalArguments.Count -ne 2 -or $attribute.NamedArguments.Count -ne 0) {throw "Selected launcher -$name range requires review"}
                    try {$minimum=$attribute.PositionalArguments[0].SafeGetValue();$maximum=$attribute.PositionalArguments[1].SafeGetValue()}
                    catch {throw "Selected launcher -$name range requires review"}
                    if($value -lt $minimum -or $value -gt $maximum) {throw "Selected launcher -$name value is outside its range"}
                    continue
                }
                '^ValidateSet(Attribute)?$' {
                    if($attribute.NamedArguments.Count -ne 0) {throw "Selected launcher -$name set requires review"}
                    try {$allowed=@($attribute.PositionalArguments|ForEach-Object {$_.SafeGetValue()})}
                    catch {throw "Selected launcher -$name set requires review"}
                    if($allowed -inotcontains [string]$value) {throw "Selected launcher -$name value is outside its set"}
                    continue
                }
                '^ValidatePattern(Attribute)?$' {
                    if($attribute.PositionalArguments.Count -ne 1 -or $attribute.NamedArguments.Count -ne 0) {throw "Selected launcher -$name pattern requires review"}
                    try {
                        $pattern=[string]$attribute.PositionalArguments[0].SafeGetValue()
                        $regex=[regex]::new($pattern,[Text.RegularExpressions.RegexOptions]::IgnoreCase,[TimeSpan]::FromSeconds(1))
                        $matched=$regex.IsMatch([string]$value)
                    } catch {throw "Selected launcher -$name pattern requires review"}
                    if(-not $matched) {throw "Selected launcher -$name value does not match its pattern"}
                    continue
                }
                '^Parameter(Attribute)?$' {continue}
                default {
                    $same=@($currentAttributes|Where-Object {$_.Extent.Text -ceq $attribute.Extent.Text}).Count -gt 0
                    if(-not $same) {throw "Selected launcher -$name validation requires review"}
                }
            }
        }
    }
}

function Assert-CreReviewedAction($Release,$Row,[string]$Xml,[string]$OldSha) {
    $doc=[Xml.XmlDocument]::new();$doc.XmlResolver=$null;$doc.LoadXml($Xml)
    $ns=[Xml.XmlNamespaceManager]::new($doc.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    $null=Get-CreTaskContract $Xml
    $principals=$doc.SelectNodes('/t:Task/t:Principals/t:Principal',$ns)
    $principal=$principals[0]
    $users=$principal.SelectNodes('t:UserId',$ns);$logons=$principal.SelectNodes('t:LogonType',$ns);$levels=$principal.SelectNodes('t:RunLevel',$ns)
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if($users.Count -ne 1 -or $users[0].InnerText -cne $sid -or $principal.SelectNodes('t:GroupId',$ns).Count -ne 0 -or
       $logons.Count -ne 1 -or $logons[0].InnerText -cne 'InteractiveToken' -or $levels.Count -gt 1 -or
       ($levels.Count -eq 1 -and $levels[0].InnerText -cne 'LeastPrivilege')) {throw 'Unsupported task principal'}
    $action=$doc.SelectSingleNode('/t:Task/t:Actions/t:Exec',$ns)
    $commands=$action.SelectNodes('t:Command',$ns);$arguments=$action.SelectNodes('t:Arguments',$ns);$workdirs=$action.SelectNodes('t:WorkingDirectory',$ns)
    if($commands.Count -ne 1 -or $arguments.Count -ne 1 -or $workdirs.Count -ne 1) {throw 'Unsupported task action'}
    $approved=@((Join-Path $env:LOCALAPPDATA 'Programs/PowerShell/7/pwsh.exe'),(Join-Path $env:ProgramFiles 'PowerShell/7/pwsh.exe'))
    $command=[IO.Path]::GetFullPath($commands[0].InnerText)
    if(@($approved|Where-Object {[IO.Path]::GetFullPath($_) -ieq $command}).Count -ne 1) {throw 'Unsupported task PowerShell executable'}
    $native=[IO.Path]::GetFullPath([string]$Release.roots.native)
    if([IO.Path]::GetFullPath($workdirs[0].InnerText) -ine $native) {throw 'Task working directory differs'}
    $argv=[string[]][CreReviewedArgs.Parser]::Parse($arguments[0].InnerText)
    $launcher=[IO.Path]::GetFullPath((Join-Path $native 'scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1'))
    if($argv.Count -lt 4 -or $argv[0] -ine '-NoProfile' -or $argv[1] -ine '-NonInteractive' -or $argv[2] -ine '-File' -or
       [IO.Path]::GetFullPath($argv[3]) -ine $launcher) {throw 'Unsupported guarded task launcher'}
    $currentArguments=Assert-CreReviewedSeriesArguments $Release $argv $launcher
    $development=ConvertTo-CreHandoffPath ([string]$Release.roots.development)
    $selectedLauncher=Join-Path $development 'scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1'
    $null=ConvertTo-CreHandoffPath $selectedLauncher
    if(-not (Test-Path -LiteralPath $selectedLauncher -PathType Leaf)) {throw 'Selected guarded launcher unavailable'}
    $selectedArguments=Assert-CreReviewedSeriesArguments $Release $argv $selectedLauncher
    Assert-CreReviewedSelectedBinding $currentArguments $selectedArguments $selectedLauncher
    $tier=Get-CreReviewedArgument $argv '-Tier'
    if((Get-CreReviewedArgument $argv '-ExpectedSha') -cne $OldSha) {throw 'Task pin differs'}
    $single=@($argv|Where-Object {$_ -ieq '-Source'}).Count;$many=@($argv|Where-Object {$_ -ieq '-Sources'}).Count
    $daumFlag=@($argv|Where-Object {$_ -ieq '-AdmitDaumVerifiedSubset'}).Count
    if($Row.key -cne 'daum-subset' -and $daumFlag -ne 0) {throw 'DAUM subset admission on wrong task'}
    if($Row.key -cne 'daum-subset' -and $currentArguments.seen.ContainsKey('-DaumSubsetPolicy')) {throw 'DAUM subset policy on wrong task'}
    if($Row.key -clike 'enrich:*') {
        $source=$Row.key.Substring(7)
        if($tier -cne 'enrich' -or $single -ne 1 -or $many -ne 0 -or (Get-CreReviewedArgument $argv '-Source') -cne $source -or $source -cnotin @($Release.supported_sources)) {throw 'Enrichment task source scope differs'}
    } elseif($tier -cne 'series') {throw 'Task tier differs'}
    elseif($Row.key -ceq 'daum-subset') {
        $allowedDaum=@('-Tier','-Source','-ExpectedSha','-WorkDeadlineSeconds','-AcknowledgeDatabaseWrites','-AdmitDaumVerifiedSubset','-DaumSubsetPolicy','-AttemptsPerSource','-Concurrency','-Distro','-LinuxUser','-LinuxRepo','-LinuxEnvFile','-AlertWebhookFile','-NotificationMode')
        foreach($key in $currentArguments.seen.Keys) {if($key -inotIn $allowedDaum) {throw 'Unsupported DAUM subset task argument'}}
        if($single -ne 1 -or $many -ne 0 -or (Get-CreReviewedArgument $argv '-Source') -cne 'daum-commercial' -or
           $daumFlag -ne 1 -or @($argv|Where-Object {$_ -iin @('-Pipeline','-AdmitCurrentInventorySource','-AdmitCurrentInventorySources','-AdmitBaselineHoldAdditively')}).Count -ne 0) {throw 'DAUM subset task scope differs'}
    }
    elseif($Row.key -ceq 'qualified') {
        if($single -ne 0 -or $many -ne 1 -or -not $Release.qualified_sources -or
           (Get-CreReviewedArgument $argv '-Sources') -cne [string]$Release.qualified_sources -or
           @($argv|Where-Object {$_ -ieq '-Pipeline'}).Count -ne 1) {throw 'Qualified task source scope differs'}
    } else {
        $expected=if($Row.key -ceq 'interra'){'interra-realty'}else{'newmark'}
        if($single -ne 1 -or $many -ne 0 -or (Get-CreReviewedArgument $argv '-Source') -cne $expected -or
           @($argv|Where-Object {$_ -ieq '-Pipeline'}).Count -ne 0) {throw 'Task source scope differs'}
    }
}

function Get-CreReviewedSecurityDescriptor([string]$Xml) {
    $doc=[Xml.XmlDocument]::new();$doc.XmlResolver=$null;$doc.LoadXml($Xml)
    $ns=[Xml.XmlNamespaceManager]::new($doc.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    $nodes=$doc.SelectNodes('/t:Task/t:RegistrationInfo/t:SecurityDescriptor',$ns)
    if($nodes.Count -gt 1) {throw 'Duplicate task security descriptor'}
    if($nodes.Count -eq 0) {return 'absent'}
    return 'present:'+$nodes[0].InnerText
}

function Get-CreReviewedTaskReadback($Row,[string]$Expected) {
    $record=Get-CreHandoffTask $Row $Expected $false $true $true
    if((Get-CreReviewedSecurityDescriptor ([string]$record.xml)) -cne (Get-CreReviewedSecurityDescriptor $Expected)) {throw 'Task security descriptor changed'}
    return $record
}

function Assert-CreReviewedTaskContract($Baseline,[string]$Actual,[bool]$Enabled) {
    $expected=Set-CreHandoffEnabledXml ([string]$Baseline.xml) $Enabled
    if((Get-CreTaskContract $Actual) -cne (Get-CreTaskContract $expected)) {throw 'Reviewed task contract changed'}
    if((Get-CreReviewedSecurityDescriptor $Actual) -cne (Get-CreReviewedSecurityDescriptor $expected)) {throw 'Task security descriptor changed'}
}

function Assert-CreReviewedFleetRowSet([object[]]$Rows) {
    $expected=@{interra='Interra Refresh';newmark='Newmark Refresh';qualified='Qualified Brokerage Pipeline'}
    if($Rows.Count -lt 3 -or @($Rows.key|Select-Object -Unique).Count -ne $Rows.Count) {throw 'Incomplete or duplicate task fleet'}
    foreach($row in $Rows) {
        $key=[string]$row.key
        if($key -ceq 'daum-subset') {$name='DAUM Verified Subset'}
        elseif($key -cmatch '^enrich:([a-z0-9]+(?:-[a-z0-9]+)*)$') {$name='Enrichment - '+$Matches[1]}
        elseif($expected.ContainsKey($key)) {$name=$expected[$key]}
        else {throw 'Unexpected task identity'}
        if($row.task_path -cne '\Agentic Assets\CRE\' -or $row.task_name -cne $name) {throw 'Unexpected task identity'}
    }
    foreach($key in $expected.Keys) {if(@($Rows|Where-Object {$_.key -ceq $key}).Count -ne 1) {throw 'Missing task identity'}}
}

function Add-CreReviewedOptionalTasks($Release) {
    $rows=@($Release.task_baseline)
    Assert-CreReviewedFleetRowSet $rows
    if($rows.Count -ne 3) {throw 'Inspection must start from the standard fleet'}
    foreach($identity in @(Get-CreHandoffRelevantTasks $Release)) {
        if($identity -ceq '\Agentic Assets\CRE\DAUM Verified Subset') {
            if('daum-commercial' -cnotin @($Release.supported_sources)) {throw 'Unsupported DAUM source'}
            $rows+=@{key='daum-subset';task_path='\Agentic Assets\CRE\';task_name='DAUM Verified Subset'}
        } elseif($identity -cmatch '^\\Agentic Assets\\CRE\\Enrichment - ([a-z0-9]+(?:-[a-z0-9]+)*)$') {
            $source=$Matches[1]
            if($source -cnotin @($Release.supported_sources)) {throw 'Unsupported enrichment source'}
            $rows+=@{key='enrich:'+$source;task_path='\Agentic Assets\CRE\';task_name='Enrichment - '+$source}
        }
    }
    Assert-CreReviewedFleetRowSet $rows
    if($Release -is [System.Collections.IDictionary]) {$Release['task_baseline']=$rows;$Release['reviewed_fleet']=$true}
    else {$Release.task_baseline=$rows;Add-Member -InputObject $Release -NotePropertyName reviewed_fleet -NotePropertyValue $true -Force}
}

function Assert-CreReviewedFleetRows($Release,$Snapshot=$null) {
    Assert-CreReviewedFleetRowSet -Rows @($Release.task_baseline)
    if($null -ne $Snapshot) {Assert-CreReviewedFleetRowSet -Rows @($Snapshot)}
}

function Assert-CreReviewedSnapshot($Release,$Snapshot,[string]$OldSha,[string]$NewSha) {
    Assert-CreReviewedFleetRows $Release $Snapshot
    $rows=@($Release.task_baseline);$saved=@($Snapshot)
    if($rows.Count -ne $saved.Count) {throw 'Task snapshot fleet differs'}
    foreach($row in $rows) {
        $matches=@($saved|Where-Object {$_.key -ceq $row.key})
        if($matches.Count -ne 1) {throw 'Task snapshot identity missing'}
        $savedRow=$matches[0]
        if($savedRow.enabled -isnot [bool] -or $savedRow.task_path -cne $row.task_path -or $savedRow.task_name -cne $row.task_name) {throw 'Task snapshot identity differs'}
        if($OldSha -cne $NewSha) {$null=Pin-ReviewedXml ([string]$savedRow.xml) $OldSha $NewSha}
        elseif($OldSha -cnotmatch '^[0-9a-f]{40}$') {throw 'Invalid exact code pin'}
        Assert-CreReviewedAction $Release $savedRow ([string]$savedRow.xml) $OldSha
        Assert-CreReviewedTaskContract $savedRow ([string]$savedRow.xml) $savedRow.enabled
    }
}

function Get-CreReviewedSnapshotDigest($Release,$Snapshot,[string]$OldSha,[string]$NewSha) {
    Assert-CreReviewedSnapshot $Release $Snapshot $OldSha $NewSha
    $parts=[Collections.Generic.List[string]]::new()
    foreach($item in @('current-task-snapshot-v1',$OldSha,$NewSha,[string]$Release.roots.native,[string]$Release.roots.runtime,[string]$Release.roots.development,[string]$Release.qualified_sources,[string]$Release.wsl.distro,[string]$Release.wsl.user,[string]$Release.wsl.env_file)) {
        $parts.Add(([Text.Encoding]::UTF8.GetByteCount($item)).ToString()+':'+$item)
    }
    if($Release.supported_sources -isnot [array] -or $Release.supported_sources.Count -eq 0 -or
       @($Release.supported_sources|Select-Object -Unique).Count -ne $Release.supported_sources.Count) {throw 'Source registry invalid'}
    foreach($source in @($Release.supported_sources|Sort-Object)) {
        if($source -isnot [string] -or $source -cnotmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$') {throw 'Source registry invalid'}
        $parts.Add(([Text.Encoding]::UTF8.GetByteCount($source)).ToString()+':'+$source)
    }
    foreach($row in @($Snapshot|Sort-Object { [string]$_.key })) {
        foreach($item in @([string]$row.key,[string]$row.task_path,[string]$row.task_name,$row.enabled.ToString(),(Get-CreHandoffHash ([string]$row.xml)))) {
            $parts.Add(([Text.Encoding]::UTF8.GetByteCount($item)).ToString()+':'+$item)
        }
    }
    return Get-CreHandoffHash ($parts -join '')
}

function Assert-CreReviewedSnapshotDigest($Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$ExpectedDigest) {
    if($ExpectedDigest -cnotmatch '^[0-9a-f]{64}$' -or (Get-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha) -cne $ExpectedDigest) {throw 'Current task snapshot binding differs'}
}

function Get-CreReviewedCurrentTaskSnapshot($Release,[string]$OldSha,[string]$NewSha) {
    # Source scope comes from the current qualified task, then is validated,
    # returned for preview and bound into the digest. No historical XML or
    # separate approval of an unchanged current source list is required.
    if($Release -isnot [System.Collections.IDictionary] -and $Release -isnot [pscustomobject]) {throw 'Current update release must be an object'}
    Add-CreReviewedOptionalTasks $Release
    Assert-CreReviewedFleetRows $Release
    Assert-CreMaintenanceIdle $Release
    $qualified=@($Release.task_baseline|Where-Object {$_.key -ceq 'qualified'})[0]
    $xml=Export-ScheduledTask -TaskPath $qualified.task_path -TaskName $qualified.task_name -ErrorAction Stop
    $doc=[Xml.XmlDocument]::new();$doc.XmlResolver=$null;$doc.LoadXml($xml)
    $ns=[Xml.XmlNamespaceManager]::new($doc.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    $nodes=$doc.SelectNodes('/t:Task/t:Actions/t:Exec/t:Arguments',$ns)
    if($nodes.Count -ne 1) {throw 'Qualified task arguments missing'}
    $argv=[string[]][CreReviewedArgs.Parser]::Parse($nodes[0].InnerText)
    $sourceText=Get-CreReviewedArgument $argv '-Sources'
    if($sourceText -cnotmatch '^[a-z0-9]+(?:-[a-z0-9]+)*(?:,[a-z0-9]+(?:-[a-z0-9]+)*)*$') {throw 'Qualified task source scope malformed'}
    if($Release.supported_sources -isnot [array]) {throw 'Source registry invalid'}
    $sources=@($sourceText.Split(','));$supported=@($Release.supported_sources)
    if($supported.Count -eq 0 -or @($sources|Select-Object -Unique).Count -ne $sources.Count) {throw 'Qualified task source scope invalid'}
    foreach($source in $sources) {if($source -cnotin $supported) {throw 'Qualified task contains unsupported source'}}
    $hasSources=if($Release -is [System.Collections.IDictionary]){$Release.Contains('qualified_sources')}else{$null -ne $Release.PSObject.Properties['qualified_sources']}
    if($hasSources -and $Release.qualified_sources -cne $sourceText) {throw 'Qualified task source scope changed'}
    if($Release -is [System.Collections.IDictionary]) {$Release['qualified_sources']=$sourceText}
    elseif($hasSources) {$Release.qualified_sources=$sourceText}
    else {Add-Member -InputObject $Release -NotePropertyName qualified_sources -NotePropertyValue $sourceText}
    return @(Get-CreReviewedTaskSnapshot $Release $OldSha $NewSha)
}

function Get-CreReviewedTaskSnapshot($Release,[string]$OldSha,[string]$NewSha) {
    Assert-CreReviewedFleetRows $Release
    Assert-CreMaintenanceIdle $Release
    $rows=@($Release.task_baseline)
    $saved=@(foreach($row in $rows) {
        $xml=Export-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop
        $record=Get-CreReviewedTaskReadback $row $xml
        if($record.enabled -isnot [bool]) {throw 'Task enabled state is not Boolean'}
        [ordered]@{key=$row.key;task_path=$row.task_path;task_name=$row.task_name;xml=[string]$record.xml;enabled=[bool]$record.enabled}
    })
    Assert-CreReviewedSnapshot $Release $saved $OldSha $NewSha
    Assert-CreMaintenanceIdle $Release
    return $saved
}

function Invoke-CreReviewedTaskHold($Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$ExpectedDigest,[scriptblock]$AssertOwner) {
    Assert-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha $ExpectedDigest
    & $AssertOwner
    Assert-CreMaintenanceIdle $Release
    # Refuse already-visible drift in any member before disabling the first.
    foreach($row in @($Snapshot)) {
        $record=Get-CreReviewedTaskReadback $row ([string]$row.xml)
        if($record.enabled -ne $row.enabled) {throw 'Task enabled state changed before hold'}
    }
    foreach($row in @($Snapshot)) {
        & $AssertOwner
        Assert-CreMaintenanceIdle $Release
        $record=Get-CreReviewedTaskReadback $row ([string]$row.xml)
        if($record.enabled -ne $row.enabled) {throw 'Task enabled state changed before hold'}
        if($row.enabled) {$null=Disable-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop}
        $held=Set-CreHandoffEnabledXml ([string]$row.xml) $false
        $record=Get-CreReviewedTaskReadback $row $held
        if($record.enabled -or $record.state -cne 'Disabled') {throw 'Task did not hold'}
    }
    Assert-CreMaintenanceIdle $Release
}

function Get-CreReviewedTaskSecurity($Task) {
    try {
        $value=$Task.GetSecurityDescriptor(7) # owner, group and DACL
        if($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value)) {return @{available=$false}}
        return @{available=$true;value=$value}
    } catch {return @{available=$false}}
}

function Invoke-CreReviewedTaskDefinitionUpdate($Service,$Row,[string]$Xml,[string]$Sid) {
    # ScheduledTasks uses a trailing separator; Schedule.Service does not.
    $folder=$Service.GetFolder(([string]$Row.task_path).TrimEnd([char]'\'))
    $before=Get-CreReviewedTaskSecurity ($folder.GetTask([string]$Row.task_name))
    # UPDATE only, do not add/remove principal ACEs, and do not run registration
    # triggers. A null SDDL preserves the existing task descriptor on update.
    # https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-tsch/849c131a-64e4-46ef-b015-9d4c599c5167
    [void]$folder.RegisterTask([string]$Row.task_name,$Xml,52,$Sid,$null,3,$null)
    $after=Get-CreReviewedTaskSecurity ($folder.GetTask([string]$Row.task_name))
    if($before.available -and $after.available) {
        if($before.value -cne $after.value) {throw 'Task security descriptor changed'}
    } else {Write-Warning 'Task security descriptor readback unavailable; null-SDDL update requested preservation.'}
}

function Update-CreReviewedTaskDefinition($Row,[string]$Xml) {
    $service=New-Object -ComObject Schedule.Service
    $service.Connect()
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Invoke-CreReviewedTaskDefinitionUpdate $service $Row $Xml $sid
}

function Invoke-CreReviewedTaskRepin($Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$ExpectedDigest,[scriptblock]$AssertOwner) {
    Assert-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha $ExpectedDigest
    & $AssertOwner
    Assert-CreMaintenanceIdle $Release
    foreach($row in @($Snapshot)) {
        $held=Set-CreHandoffEnabledXml ([string]$row.xml) $false
        $record=Get-CreReviewedTaskReadback $row $held
        if($record.enabled -or $record.state -cne 'Disabled') {throw 'Fleet is not held'}
    }
    foreach($row in @($Snapshot)) {
        & $AssertOwner
        Assert-CreMaintenanceIdle $Release
        $held=Set-CreHandoffEnabledXml ([string]$row.xml) $false
        $record=Get-CreReviewedTaskReadback $row $held
        if($record.enabled -or $record.state -cne 'Disabled') {throw 'Task is not held'}
        $pinned=Set-CreHandoffEnabledXml (Pin-ReviewedXml ([string]$row.xml) $OldSha $NewSha) $false
        Update-CreReviewedTaskDefinition $row $pinned
        $record=Get-CreReviewedTaskReadback $row $pinned
        if($record.enabled -or $record.state -cne 'Disabled') {throw 'Repinned task is not held'}
    }
    Assert-CreMaintenanceIdle $Release
}

function Invoke-CreReviewedTaskRestore($Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$ExpectedDigest,[scriptblock]$AssertOwner) {
    Assert-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha $ExpectedDigest
    & $AssertOwner
    Assert-CreMaintenanceIdle $Release
    # Verify every member before enabling the first. A successor can start as
    # soon as an enabled task is restored; never stop it or demand idle again.
    foreach($row in @($Snapshot)) {
        $pinned=if($OldSha -ceq $NewSha){[string]$row.xml}else{Pin-ReviewedXml ([string]$row.xml) $OldSha $NewSha}
        $held=Set-CreHandoffEnabledXml $pinned $false
        $record=Get-CreReviewedTaskReadback $row $held
        if($record.enabled -or $record.state -cne 'Disabled') {throw 'Repinned fleet changed before restore'}
    }
    foreach($row in @($Snapshot)) {
        & $AssertOwner
        Assert-CreHandoffFleet $Release
        $pinned=if($OldSha -ceq $NewSha){[string]$row.xml}else{Pin-ReviewedXml ([string]$row.xml) $OldSha $NewSha}
        $held=Set-CreHandoffEnabledXml $pinned $false
        $record=Get-CreReviewedTaskReadback $row $held
        if($record.enabled -or $record.state -cne 'Disabled') {throw 'Task changed during restore'}
        if($row.enabled) {$null=Enable-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop}
        $expected=Set-CreHandoffEnabledXml $pinned $row.enabled
        $record=Get-CreReviewedTaskReadback $row $expected
        if($record.enabled -ne $row.enabled) {throw 'Restored enabled state differs'}
    }
}

# This is only the task side of reconciliation. Its caller must independently
# recheck both old Git checkouts and owner identity in AssertUnchanged. The
# callback must allow a scheduled successor after the first task is enabled.
function Invoke-CreReviewedTaskReconcileHold($Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$ExpectedDigest,[scriptblock]$AssertUnchanged) {
    Assert-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha $ExpectedDigest
    & $AssertUnchanged
    Assert-CreMaintenanceIdle $Release
    foreach($row in @($Snapshot)) {
        $xml=Export-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop
        $record=Get-CreReviewedTaskReadback $row $xml
        if($record.enabled -isnot [bool] -or ($record.enabled -and -not $row.enabled)) {throw 'Unexpected task state during hold reconciliation'}
        Assert-CreReviewedTaskContract $row $record.xml $record.enabled
    }
    foreach($row in @($Snapshot)) {
        & $AssertUnchanged
        $xml=Export-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop
        $record=Get-CreReviewedTaskReadback $row $xml
        if($record.enabled -isnot [bool] -or ($record.enabled -and -not $row.enabled)) {throw 'Unexpected task state during hold reconciliation'}
        Assert-CreReviewedTaskContract $row $record.xml $record.enabled
        if($row.enabled -and -not $record.enabled) {
            $null=Enable-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop
        }
        $expected=Set-CreHandoffEnabledXml ([string]$row.xml) $row.enabled
        $record=Get-CreReviewedTaskReadback $row $expected
        if($record.enabled -ne $row.enabled) {throw 'Original task flag not restored'}
    }
    & $AssertUnchanged
    foreach($row in @($Snapshot)) {
        $expected=Set-CreHandoffEnabledXml ([string]$row.xml) $row.enabled
        $record=Get-CreReviewedTaskReadback $row $expected
        if($record.enabled -ne $row.enabled) {throw 'Original task flag changed'}
    }
}



function Get-CreReviewedSettledTask($Row,[string]$OldSha,[string]$NewSha,[bool]$AllowEnabled) {
    $actual=Export-ScheduledTask -TaskPath $Row.task_path -TaskName $Row.task_name -ErrorAction Stop
    $record=Get-CreReviewedTaskReadback $Row $actual
    if($record.enabled -isnot [bool] -or ($record.enabled -and (-not $AllowEnabled -or -not $Row.enabled))) {throw 'Unexpected task flag'}
    if((-not $record.enabled -and $record.state -cne 'Disabled') -or ($record.enabled -and $record.state -cnotin @('Ready','Running','Queued'))) {throw 'Unexpected task state'}
    $old=Set-CreHandoffEnabledXml ([string]$Row.xml) $record.enabled
    $new=if($OldSha -ceq $NewSha){$old}else{Set-CreHandoffEnabledXml (Pin-ReviewedXml ([string]$Row.xml) $OldSha $NewSha) $record.enabled}
    if((Get-CreReviewedSecurityDescriptor $record.xml) -cne (Get-CreReviewedSecurityDescriptor $old)) {throw 'Task security descriptor changed'}
    $contract=Get-CreTaskContract ([string]$record.xml)
    if($contract -ceq (Get-CreTaskContract $new)) {return @{record=$record;pin='selected'}}
    if($contract -ceq (Get-CreTaskContract $old)) {return @{record=$record;pin='old'}}
    throw 'Task definition differs from both captured pins'
}

function Complete-CreReviewedRepin($Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$Digest,[scriptblock]$AssertOwner) {
    Assert-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha $Digest
    & $AssertOwner
    Assert-CreMaintenanceIdle $Release
    foreach($row in @($Snapshot)) {$null=Get-CreReviewedSettledTask $row $OldSha $NewSha $false}
    foreach($row in @($Snapshot)) {
        & $AssertOwner
        Assert-CreMaintenanceIdle $Release
        $current=Get-CreReviewedSettledTask $row $OldSha $NewSha $false
        if($current.pin -ceq 'old') {
            $wanted=Set-CreHandoffEnabledXml (Pin-ReviewedXml ([string]$row.xml) $OldSha $NewSha) $false
            Update-CreReviewedTaskDefinition $row $wanted
        }
        if((Get-CreReviewedSettledTask $row $OldSha $NewSha $false).pin -cne 'selected') {throw 'Task pin did not complete'}
    }
}

function Complete-CreReviewedRestore($Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$Digest,[scriptblock]$AssertOwner) {
    Assert-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha $Digest
    & $AssertOwner
    # A restored successor may already be running. Never stop it or require idle.
    foreach($row in @($Snapshot)) {
        if((Get-CreReviewedSettledTask $row $OldSha $NewSha $true).pin -cne 'selected') {throw 'Task pin is not selected'}
    }
    foreach($row in @($Snapshot)) {
        & $AssertOwner
        Assert-CreHandoffFleet $Release
        $current=Get-CreReviewedSettledTask $row $OldSha $NewSha $true
        if($current.pin -cne 'selected') {throw 'Task pin changed during restore'}
        if($row.enabled -and -not $current.record.enabled) {$null=Enable-ScheduledTask -TaskPath $row.task_path -TaskName $row.task_name -ErrorAction Stop}
        $actual=Get-CreReviewedSettledTask $row $OldSha $NewSha $true
        if($actual.pin -cne 'selected' -or $actual.record.enabled -ne $row.enabled) {throw 'Task flag did not restore'}
    }
}

function Invoke-CreReviewedTaskStage([string]$Stage,$Release,$Snapshot,[string]$OldSha,[string]$NewSha,[string]$Digest,[scriptblock]$AssertOwner) {
    & $AssertOwner
    if($Stage -ceq 'Inspect') {
        $saved=@(Get-CreReviewedCurrentTaskSnapshot $Release $OldSha $NewSha)
        return @{snapshot=$saved;task_baseline=@($Release.task_baseline);qualified_sources=$Release.qualified_sources;digest=(Get-CreReviewedSnapshotDigest $Release $saved $OldSha $NewSha)}
    }
    Assert-CreReviewedSnapshotDigest $Release $Snapshot $OldSha $NewSha $Digest
    Assert-CreHandoffFleet $Release
    switch($Stage) {
        'Hold' {Invoke-CreReviewedTaskHold $Release $Snapshot $OldSha $NewSha $Digest $AssertOwner}
        'Repin' {Invoke-CreReviewedTaskRepin $Release $Snapshot $OldSha $NewSha $Digest $AssertOwner}
        'CompleteRepin' {Complete-CreReviewedRepin $Release $Snapshot $OldSha $NewSha $Digest $AssertOwner}
        'CompleteRestore' {Complete-CreReviewedRestore $Release $Snapshot $OldSha $NewSha $Digest $AssertOwner}
        'ReconcileHold' {Invoke-CreReviewedTaskReconcileHold $Release $Snapshot $OldSha $NewSha $Digest $AssertOwner}
        'Restore' {Invoke-CreReviewedTaskRestore $Release $Snapshot $OldSha $NewSha $Digest $AssertOwner}
        'VerifyHeld' {
            foreach($row in @($Snapshot)) {
                & $AssertOwner
                $record=Get-CreReviewedTaskReadback $row (Set-CreHandoffEnabledXml ([string]$row.xml) $false)
                if($record.enabled -or $record.state -cne 'Disabled') {throw 'Fleet is not held at the old pin'}
            }
        }
        'VerifyOriginal' {
            foreach($row in @($Snapshot)) {
                & $AssertOwner
                $record=Get-CreReviewedTaskReadback $row (Set-CreHandoffEnabledXml ([string]$row.xml) $row.enabled)
                if($record.enabled -ne $row.enabled) {throw 'Original fleet differs'}
            }
        }
        'VerifyRestored' {
            foreach($row in @($Snapshot)) {
                & $AssertOwner
                $xml=if($OldSha -ceq $NewSha){[string]$row.xml}else{Pin-ReviewedXml ([string]$row.xml) $OldSha $NewSha}
                $record=Get-CreReviewedTaskReadback $row (Set-CreHandoffEnabledXml $xml $row.enabled)
                if($record.enabled -ne $row.enabled) {throw 'Restored fleet differs'}
            }
        }
        default {throw 'Unknown reviewed task stage'}
    }
    & $AssertOwner
    return @{stage=$Stage;complete=$true}
}
