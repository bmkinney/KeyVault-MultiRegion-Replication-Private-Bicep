using '../main.bicep'

param workload = 'mrkv'
param primaryLocation = 'eastus'
param secondaryLocation = 'centralus'
param primaryVaultResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-primary/providers/Microsoft.KeyVault/vaults/example-primary'
param secondaryVaultResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-secondary/providers/Microsoft.KeyVault/vaults/example-secondary'
// If a vault already publishes events, supply its system topic ID; otherwise leave empty.
param primarySystemTopicResourceId = ''
param secondarySystemTopicResourceId = ''
param secretNames = ['application-password']
param secretPrefix = 'demo-'
param replicateAllSecrets = false
param allowUnmanagedOverwrite = false
param enableWorker = false
param enableEventRouting = false
param authoritativeRegion = 'primary'
