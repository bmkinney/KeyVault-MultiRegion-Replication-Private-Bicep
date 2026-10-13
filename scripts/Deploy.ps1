[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [string]$ParametersFile = "$PSScriptRoot\..\infra\parameters\example.bicepparam",
    [string]$DeploymentLocation = 'eastus',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
Get-Command az -ErrorAction Stop | Out-Null
Get-Command python -ErrorAction Stop | Out-Null
$ParametersFile = (Resolve-Path $ParametersFile).Path
$Template = (Resolve-Path "$PSScriptRoot\..\infra\main.bicep").Path
$Name = 'mrkv-infrastructure'

$CompiledParameters = [System.IO.Path]::GetTempFileName()
try {
    & az bicep build-params --file $ParametersFile --outfile $CompiledParameters --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Parameter compilation failed; no deployment attempted.' }
    & python "$PSScriptRoot\check_vault_integration.py" --parameters $CompiledParameters --subscription $SubscriptionId
    if ($LASTEXITCODE -ne 0) { throw 'Vault integration preflight failed; no deployment attempted.' }
} finally {
    Remove-Item -LiteralPath $CompiledParameters -Force
}

& az deployment sub validate --subscription $SubscriptionId --location $DeploymentLocation `
    --name $Name --template-file $Template --parameters $ParametersFile --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'ARM validation failed; no deployment attempted.' }
& az deployment sub what-if --subscription $SubscriptionId --location $DeploymentLocation `
    --name $Name --template-file $Template --parameters $ParametersFile --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'What-if failed; no deployment attempted.' }
if (-not $Apply) {
    Write-Output 'Preview only. Review changes, then explicitly invoke -Apply if authorized.'
    return
}
& az deployment sub create --subscription $SubscriptionId --location $DeploymentLocation `
    --name $Name --template-file $Template --parameters $ParametersFile --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Deployment failed. Inspect deployment operations before retrying.' }
