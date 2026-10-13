[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourceVaultUri,
    [Parameter(Mandatory)][string]$DestinationVaultUri,
    [switch]$CreateSyntheticSecret
)
$ErrorActionPreference = 'Stop'
if (-not $CreateSyntheticSecret) { throw 'This live test creates a retained synthetic secret. Explicitly supply -CreateSyntheticSecret.' }
$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw 'Create the local .venv and install src\replication\requirements.txt first.' }
& $Python "$PSScriptRoot\..\tests\integration\smoke.py" --source $SourceVaultUri `
    --destination $DestinationVaultUri --timeout 60 --confirm-create-test-secret
if ($LASTEXITCODE -ne 0) { throw 'Live replication acceptance test failed.' }
