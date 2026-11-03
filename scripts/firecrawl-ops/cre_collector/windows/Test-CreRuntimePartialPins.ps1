# Synthetic native mixed-pin regression; no Scheduler or COM calls.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'CreCodeHandoff.ps1')
$old='a'*40; $intermediate='b'*40; $new='c'*40
$xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>Reviewed '+$intermediate+'</Description></RegistrationInfo><Actions><Exec><Command>pwsh.exe</Command><Arguments>-ExpectedSha '+$intermediate+'</Arguments></Exec></Actions><Settings><Enabled>false</Enabled></Settings></Task>'
function Read-CreHandoffBaselineXml {param($Release,$Row);return $xml}
function Get-CreHandoffTask {param($Row,$Expected,$A,$B); if($Expected -cne $xml.Replace($intermediate,$new)){throw 'Wrong selected XML'}; return @{enabled=$false;state='Disabled';xml=$Expected}}
$release=@{old=@{sha=$old};new=@{sha=$new}}
$row=@{enabled=$false;pin_before=$intermediate}
$actual=Get-CreMaintenanceTask $release $row $true
if($actual.xml -cne $xml.Replace($intermediate,$new)){throw 'Mixed pin not preserved'}
'CRE partial pin native fixture: passed'
