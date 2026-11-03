using '../main.bicep'

param workload = 'mrkv'
param primaryLocation = 'eastus'
param secondaryLocation = 'centralus'
param managePermissions = true
param provisioningIdentityResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/platform-identities/providers/Microsoft.ManagedIdentity/userAssignedIdentities/example-provisioner'
// The identity may be in another subscription of the same tenant. Leave the next two unset
// to run in the primary solution group; set a group only if it exists in the deployment subscription.
// param provisioningResourceGroupName = 'example-provisioning'
// param provisioningLocation = 'eastus'
param provisioningTimeoutSeconds = 1800

// Empty IDs create workers/vaults/topics under the provisioning identity.
// Supply existing IDs to reference those resources instead.
param primaryWorkerIdentityResourceId = ''
param secondaryWorkerIdentityResourceId = ''
param primaryVaultResourceId = ''
param secondaryVaultResourceId = ''
param primarySystemTopicResourceId = ''
param secondarySystemTopicResourceId = ''
// Reuse a Log Analytics workspace (any subscription of the same tenant) instead of creating one per
// region, for example when policy blocks workspace creation. Both regions may share one workspace.
// param primaryLogAnalyticsWorkspaceResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/monitoring/providers/Microsoft.OperationalInsights/workspaces/shared'
// param secondaryLogAnalyticsWorkspaceResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/monitoring/providers/Microsoft.OperationalInsights/workspaces/shared'
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

// Networking. The default (new) creates a dedicated VNet and private DNS zones in each region.
// To reuse existing VNets and DNS instead, so no new address space or firewall rules are needed, uncomment
// and edit one block per region. VNets must be in this subscription and in primaryLocation/secondaryLocation.
// existing: reuse an empty subnet delegated to Microsoft.App/environments (/27 or larger) for the Function app
//           and an undelegated subnet for this deployment's private endpoints.
// newSubnet: reuse the VNet and add the delegated integration subnet at a free /27 or larger prefix.
// param primaryNetworkMode = 'existing'
// param primaryVnetName = 'vnet-hub-eus'
// param primaryVnetResourceGroupName = 'rg-network-eus'
// param primaryIntegrationSubnetName = 'snet-mrkv-integration'
// newSubnet only: param primaryIntegrationSubnetPrefix = '10.50.1.0/27'
// param primaryPrivateEndpointSubnetName = 'snet-private-endpoints'
// Reused vaults that need an endpoint in this VNet (solution-created vaults always get one):
// param primaryEndpointsToCreate = ['primaryVault', 'secondaryVault']
// Existing privatelink zones that receive the endpoint records. Omit a key when policy creates the records.
// param primaryPrivateDnsZoneIds = {
//   vault: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net'
//   bus: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.servicebus.windows.net'
//   blob: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net'
//   queue: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.queue.core.windows.net'
//   table: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.table.core.windows.net'
//   app: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.azurewebsites.net'
//   monitor: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.monitor.azure.com'
//   oms: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.oms.opinsights.azure.com'
//   ods: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.ods.opinsights.azure.com'
//   agent: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.agentsvc.azure-automation.net'
// }
//
// param secondaryNetworkMode = 'existing'
// param secondaryVnetName = 'vnet-hub-cus'
// param secondaryVnetResourceGroupName = 'rg-network-cus'
// param secondaryIntegrationSubnetName = 'snet-mrkv-integration'
// newSubnet only: param secondaryIntegrationSubnetPrefix = '10.60.1.0/27'
// param secondaryPrivateEndpointSubnetName = 'snet-private-endpoints'
// param secondaryEndpointsToCreate = ['primaryVault', 'secondaryVault']
// param secondaryPrivateDnsZoneIds = { ...same keys as primaryPrivateDnsZoneIds, zones that resolve from the secondary VNet... }
