# Execute the shipped policy argument statements without dispatching WSL.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'Invoke-CreGuardedTier.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Launcher parse failure.' }
$forwarders = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Extent.Text -match "arguments \+=" -and
    $node.Extent.Text -match '--(?:admit-daum-verified-subset|daum-subset-policy)' -and
    $node.Parent -is [System.Management.Automation.Language.StatementBlockAst]
}, $true) | Where-Object { $_.Extent.Text -notmatch 'selectedSourceArgument' })
if ($forwarders.Count -ne 2) { throw 'Expected exactly two policy forwarding statements.' }
$defaults = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Extent.Text -match "PSBoundParameters.ContainsKey\('DaumSubsetPolicy'\)" -and
    $node.Extent.Text -match "tenure-and-empty-title-v2"
}, $true))
if ($defaults.Count -ne 1) { throw 'Expected exactly one DAUM default selector.' }
foreach ($admitted in @($false, $true)) {
    $AdmitDaumVerifiedSubset = $admitted
    $DaumSubsetPolicy = ''
    . ([ScriptBlock]::Create($defaults[0].Extent.Text))
    $expected = if ($admitted) { 'tenure-and-empty-title-v2' } else { 'tenure-only-v1' }
    if ($DaumSubsetPolicy -cne $expected) { throw 'DAUM default policy differs.' }
}
foreach ($policy in @('tenure-only-v1', 'tenure-and-empty-title-v2')) {
    $DaumSubsetPolicy = $policy
    $AdmitDaumVerifiedSubset = $true
    $arguments = @()
    foreach ($node in $forwarders) { . ([ScriptBlock]::Create($node.Extent.Text)) }
    if ($arguments[0] -cne '--admit-daum-verified-subset') { throw 'Missing existing opt-in.' }
    if ($arguments.Count -ne 3 -or $arguments[1] -cne '--daum-subset-policy' -or $arguments[2] -cne $policy) {
        throw 'Selected policy not forwarded exactly.'
    }
}
$refused = $false
try { & $launcher -DaumSubsetPolicy tenure-and-empty-title-v2 -CheckOnly | Out-Null }
catch {
    if ($_.Exception.Message -notmatch 'requires -AdmitDaumVerifiedSubset') { throw }
    $refused = $true
}
if (-not $refused) { throw 'V2 without existing opt-in did not refuse.' }
Write-Output 'DAUM native policy: PASS (V1/V2 exact forwarding, invalid opt-in refuses before dispatch)'
