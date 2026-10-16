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
$CliVersionJson = & az version --output json --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Unable to determine Azure CLI version.' }
$CliVersion = ($CliVersionJson | ConvertFrom-Json).'azure-cli'
if ([version]$CliVersion -lt [version]'2.75.0') {
    throw 'Azure CLI 2.75.0 or newer is required to expose what-if coverage diagnostics.'
}
$ParametersFile = (Resolve-Path $ParametersFile).Path
$Template = (Resolve-Path "$PSScriptRoot\..\infra\main.bicep").Path
$Name = 'mrkv-infrastructure'

$CompiledParameters = [System.IO.Path]::GetTempFileName()
$PreparedParameters = [System.IO.Path]::GetTempFileName()
$WhatIfFile = [System.IO.Path]::GetTempFileName()
try {
    & az bicep build-params --file $ParametersFile --outfile $CompiledParameters --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Parameter compilation failed; no deployment attempted.' }
    & python "$PSScriptRoot\check_vault_integration.py" --parameters $CompiledParameters `
        --subscription $SubscriptionId --output-parameters $PreparedParameters
    if ($LASTEXITCODE -ne 0) { throw 'Vault integration preflight failed; no deployment attempted.' }

    & az deployment sub validate --subscription $SubscriptionId --location $DeploymentLocation `
        --name $Name --template-file $Template --parameters $PreparedParameters --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'ARM validation failed; no deployment attempted.' }
    & az deployment sub what-if --subscription $SubscriptionId --location $DeploymentLocation `
        --name $Name --template-file $Template --parameters $PreparedParameters --only-show-errors `
        --no-pretty-print --result-format FullResourcePayloads --output json > $WhatIfFile
    if ($LASTEXITCODE -ne 0) { throw 'What-if failed; no deployment attempted.' }
    $Preview = Get-Content -LiteralPath $WhatIfFile -Raw | ConvertFrom-Json -Depth 100
    Write-Output ($Preview | ConvertTo-Json -Depth 100)
    if ($Preview.status -ne 'Succeeded' -or $null -ne $Preview.error) {
        throw 'What-if did not succeed; no deployment attempted.'
    }
    $Result = if ($null -ne $Preview.properties) { $Preview.properties } else { $Preview }
    if ($null -ne $Result.error) { throw 'What-if returned an error; no deployment attempted.' }
    if (-not ($Result.changes -is [System.Array])) {
        throw 'What-if returned an unrecognized changes payload; no deployment attempted.'
    }
    $Diagnostics = @(@($Preview.diagnostics) + @($Result.diagnostics) | Where-Object { $null -ne $_ })
    $Incomplete = @($Result.changes | Where-Object {
        $_.changeType -notin @('Create', 'Modify', 'Delete', 'NoChange') -or
        [string]::IsNullOrWhiteSpace($_.resourceId) -or
        -not [string]::IsNullOrWhiteSpace($_.unsupportedReason)
    })
    if (
        $Diagnostics.Count -gt 0 -or $Incomplete.Count -gt 0 -or
        @($Result.potentialChanges | Where-Object { $null -ne $_ }).Count -gt 0 -or
        @($Preview.potentialChanges | Where-Object { $null -ne $_ }).Count -gt 0 -or
        $Preview.nextLink -or $Result.nextLink
    ) {
        throw 'What-if coverage is incomplete (diagnostics, ignored/unsupported resources, or additional results). No deployment attempted.'
    }
    if (-not $Apply) {
        Write-Output 'Preview only. Review changes, then explicitly invoke -Apply if authorized.'
        return
    }
    & az deployment sub create --subscription $SubscriptionId --location $DeploymentLocation `
        --name $Name --template-file $Template --parameters $PreparedParameters --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Deployment failed. Inspect deployment operations before retrying.' }
} finally {
    Remove-Item -LiteralPath $CompiledParameters, $PreparedParameters, $WhatIfFile -Force
}
