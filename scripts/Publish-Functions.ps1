[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$AppName
)
$ErrorActionPreference = 'Stop'
Get-Command az, func -ErrorAction Stop | Out-Null
$MetadataText = & az functionapp show --subscription $SubscriptionId --resource-group $ResourceGroup `
    --name $AppName --query '{host:defaultHostName,hosts:enabledHostNames,public:publicNetworkAccess}' --output json --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect target Function App.' }
$Metadata = $MetadataText | ConvertFrom-Json
if ($Metadata.public -ne 'Disabled') { throw 'Expected public network access disabled; refusing an unexpected target.' }
$ScmHosts = @($Metadata.hosts | Where-Object { $_ -match '\.scm\.' })
if ($ScmHosts.Count -eq 0) { throw 'Cannot identify the app SCM hostname for private DNS verification.' }
foreach ($HostName in (@($Metadata.host) + $ScmHosts)) {
    $Addresses = [System.Net.Dns]::GetHostAddresses($HostName)
    if (-not $Addresses -or $Addresses.Where({
        $Bytes = $_.GetAddressBytes()
        $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
        ($Bytes[0] -eq 10 -or ($Bytes[0] -eq 172 -and $Bytes[1] -ge 16 -and $Bytes[1] -le 31) -or
         ($Bytes[0] -eq 192 -and $Bytes[1] -eq 168))
    }).Count -ne $Addresses.Count) {
        throw "$HostName must resolve exclusively to private IPv4 addresses on this deployment machine."
    }
}
Push-Location "$PSScriptRoot\..\src\replication"
try {
    & func azure functionapp publish $AppName --python --subscription $SubscriptionId
    if ($LASTEXITCODE -ne 0) { throw 'Private code publication failed. Do not enable public access as a workaround.' }
} finally {
    Pop-Location
}
& az functionapp function list --subscription $SubscriptionId --resource-group $ResourceGroup `
    --name $AppName --query '[].name' --output table --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Code published, but function discovery verification failed.' }
