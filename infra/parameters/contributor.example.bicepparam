using '../main.bicep'

param workload = 'mrkv'
param primaryLocation = 'eastus'
param secondaryLocation = 'centralus'
param managePermissions = false
param primaryWorkerIdentityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/platform-identities/providers/Microsoft.ManagedIdentity/userAssignedIdentities/example-primary-worker'
param secondaryWorkerIdentityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/platform-identities/providers/Microsoft.ManagedIdentity/userAssignedIdentities/example-secondary-worker'
param primaryVaultResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-primary/providers/Microsoft.KeyVault/vaults/example-primary'
param secondaryVaultResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-secondary/providers/Microsoft.KeyVault/vaults/example-secondary'
param primarySystemTopicResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-primary/providers/Microsoft.EventGrid/systemTopics/example-primary-events'
param secondarySystemTopicResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-secondary/providers/Microsoft.EventGrid/systemTopics/example-secondary-events'
param secretNames = ['application-password']
param replicateAllSecrets = false
param allowUnmanagedOverwrite = false
param enableWorker = false
param enableEventRouting = false
param authoritativeRegion = 'primary'
