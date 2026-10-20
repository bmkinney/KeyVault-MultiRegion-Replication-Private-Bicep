using '../main.bicep'

param workload = 'mrkv'
param primaryLocation = 'eastus'
param secondaryLocation = 'centralus'
param managePermissions = true
param provisioningIdentityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/platform-identities/providers/Microsoft.ManagedIdentity/userAssignedIdentities/example-provisioner'
param provisioningResourceGroupName = 'example-provisioning'
param provisioningLocation = 'eastus'
param provisioningTimeoutSeconds = 1800

// Empty IDs create workers/vaults/topics under the provisioning identity.
// Supply existing IDs to reference those resources instead.
param primaryWorkerIdentityResourceId = ''
param secondaryWorkerIdentityResourceId = ''
param primaryVaultResourceId = ''
param secondaryVaultResourceId = ''
param primarySystemTopicResourceId = ''
param secondarySystemTopicResourceId = ''
param primaryVaultAuthorizationModel = 'accessPolicy'
param secondaryVaultAuthorizationModel = 'accessPolicy'

// Required only when a selected vault uses RBAC. Administrators predefine this
// custom role with exactly the four documented non-destructive secret data actions.
param replicaWriterRoleResourceId = ''
param secretNames = ['application-password']
param replicateAllSecrets = false
param allowUnmanagedOverwrite = false
param enableWorker = false
param enableEventRouting = false
param authoritativeRegion = 'primary'
