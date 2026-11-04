# Inert shared Task Scheduler XML contract; no scheduler calls.
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
        @('/t:Task/t:Settings','AllowHardTerminate','true'),
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
