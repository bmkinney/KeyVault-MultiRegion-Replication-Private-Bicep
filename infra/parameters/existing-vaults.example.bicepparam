using '../main.bicep'

param workload = 'mrkv'
param primaryLocation = 'eastus'
param secondaryLocation = 'centralus'
// Leave empty to create workers, or supply existing user-assigned identity ARM IDs.
param primaryWorkerIdentityResourceId = ''
param secondaryWorkerIdentityResourceId = ''
// For Contributor-only deployment use contributor.example.bicepparam and administrator-provisioned grants.
param managePermissions = true
param primaryVaultResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-primary/providers/Microsoft.KeyVault/vaults/example-primary'
param secondaryVaultResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing-secondary/providers/Microsoft.KeyVault/vaults/example-secondary'
// Existing vaults auto-detect RBAC or access policies; these models apply only when an ID is empty.
param primaryVaultAuthorizationModel = 'accessPolicy'
param secondaryVaultAuthorizationModel = 'accessPolicy'
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
