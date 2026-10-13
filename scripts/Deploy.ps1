[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [string]$ParametersFile = "$PSScriptRoot\..\infra\parameters\example.bicepparam",
    [string]$DeploymentLocation = 'eastus',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
Get-Command az -ErrorAction Stop | Out-Null
$ParametersFile = (Resolve-Path $ParametersFile).Path
$Template = (Resolve-Path "$PSScriptRoot\..\infra\main.bicep").Path
$Name = 'mrkv-infrastructure'

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
