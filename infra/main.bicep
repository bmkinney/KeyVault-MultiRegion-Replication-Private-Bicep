targetScope = 'subscription'

@description('Lowercase alphanumeric workload prefix, 3-8 characters.')
@minLength(3)
@maxLength(8)
param workload string = 'mrkv'

@description('Primary region; not paired with the example secondary region.')
param primaryLocation string = 'eastus'

@description('Secondary region.')
param secondaryLocation string = 'centralus'

@description('Existing primary vault ARM ID in this subscription; empty creates a vault.')
param primaryVaultResourceId string = ''

@description('Existing secondary vault ARM ID in this subscription; empty creates a vault.')
param secondaryVaultResourceId string = ''

@description('Existing primary worker user-assigned identity ARM ID in any subscription in this tenant; empty creates one in managed-permissions mode.')
param primaryWorkerIdentityResourceId string = ''

@description('Existing secondary worker user-assigned identity ARM ID in any subscription in this tenant; empty creates one in managed-permissions mode.')
param secondaryWorkerIdentityResourceId string = ''

@description('Create role definitions, assignments, and vault policies. False leaves IAM to administrators and requires existing workers, vaults, and system topics.')
param managePermissions bool = true

@description('Precreated provisioning UAMI in any subscription in this tenant for keyless delegated bootstrap/IAM via Deploy.ps1. Empty retains the selected existing workflow.')
param provisioningIdentityResourceId string = ''

@description('Optional override: existing resource group in the deployment subscription for delegated provisioning containers. Empty uses the primary solution group.')
param provisioningResourceGroupName string = ''

@description('ACI execution region; management-plane execution only, no private vault access. The runner defaults to primaryLocation when unset.')
param provisioningLocation string = 'eastus'

@description('Maximum time to wait for each delegated job, including Azure deployment operations.')
@minValue(120)
@maxValue(7200)
param provisioningTimeoutSeconds int = 1800

@description('Administrator-predefined restricted replica-writer role ARM ID. Required in delegated mode when either vault uses RBAC.')
param replicaWriterRoleResourceId string = ''

@description('Internal ownership flags refreshed by delegated orchestration for solution-created parent diagnostics.')
param resolvedOwnedFoundationDiagnostics object = {}

@description('Internal existing-worker identity metadata populated by Deploy.ps1; do not supply principal/client IDs manually.')
param resolvedWorkerIdentities object = {}

@description('Authorization for a solution-managed primary vault. Reused vaults retain their detected model.')
@allowed(['accessPolicy', 'rbac'])
param primaryVaultAuthorizationModel string = 'accessPolicy'

@description('Authorization for a solution-managed secondary vault. Reused vaults retain their detected model.')
@allowed(['accessPolicy', 'rbac'])
param secondaryVaultAuthorizationModel string = 'accessPolicy'

@description('Internal read-only discovery snapshot, populated by Deploy.ps1. Do not set in user parameter files.')
param resolvedExistingVaults object = {}

@description('Internal catalogue preserving previously deployed vault role-assignment names, populated by Deploy.ps1.')
param resolvedVaultRoleAssignments object = {}

@description('Existing system topic for the primary vault; preflight requires reuse when one exists.')
param primarySystemTopicResourceId string = ''

@description('Existing system topic for the secondary vault.')
param secondarySystemTopicResourceId string = ''

@description('Case-insensitive explicit secret-name allowlist. Combined with prefix by union.')
param secretNames array = []

@description('Case-insensitive secret-name prefix; empty does not select anything.')
#disable-next-line secure-secrets-in-params // Name filter, never a secret value.
param secretPrefix string = ''

@description('Explicit whole-vault opt-in. Defaults true only when both vaults are new.')
param replicateAllSecrets bool = empty(primaryVaultResourceId) && empty(secondaryVaultResourceId)

@description('Explicit approval to replace/disable unmanaged destination secrets within the selection.')
param allowUnmanagedOverwrite bool = false

@description('Primary VNet, integration subnet, and private endpoint subnet CIDRs.')
param primaryNetwork object = {
  address: '10.35.0.0/16'
  integration: '10.35.1.0/24'
  endpoints: '10.35.0.0/24'
}

@description('Secondary VNet, integration subnet, and private endpoint subnet CIDRs.')
param secondaryNetwork object = {
  address: '10.36.0.0/16'
  integration: '10.36.1.0/24'
  endpoints: '10.36.0.0/24'
}

@description('Notification recipient. Empty creates alerts without email delivery; set before operational use.')
param alertEmail string = ''

@description('Activate event subscriptions only after publishing and verifying both workers.')
param enableEventRouting bool = false

@description('Create the event subscriptions. Delegated runs defer them until the topic identities hold their delivery grants.')
param deployEventRouting bool = true

@description('Enable the authoritative worker only after publishing. Never enable both workers.')
param enableWorker bool = false

@description('Authoritative region. Change only after the old writer/worker is fenced and recovery is approved.')
@allowed(['primary', 'secondary'])
param authoritativeRegion string = 'primary'

@description('Resource tags.')
param tags object = {
  workload: workload
  purpose: 'private-secret-replication-demo'
}

var token = take(uniqueString(subscription().id, workload), 8)
var primaryPrefix = '${workload}-p-${token}'
var secondaryPrefix = '${workload}-s-${token}'

resource primaryGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: 'rg-${primaryPrefix}'
  location: primaryLocation
  tags: tags
}

resource secondaryGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: 'rg-${secondaryPrefix}'
  location: secondaryLocation
  tags: tags
}

var zoneNames = {
  vault: 'privatelink.vaultcore.azure.net'
  bus: 'privatelink.servicebus.windows.net'
  blob: 'privatelink.blob.${environment().suffixes.storage}'
  queue: 'privatelink.queue.${environment().suffixes.storage}'
  table: 'privatelink.table.${environment().suffixes.storage}'
  app: 'privatelink.azurewebsites.net'
  monitor: 'privatelink.monitor.azure.com'
  oms: 'privatelink.oms.opinsights.azure.com'
  ods: 'privatelink.ods.opinsights.azure.com'
  agent: 'privatelink.agentsvc.azure-automation.net'
}

var stampInputs = [
  {
    prefix: primaryPrefix
    groupName: primaryGroup.name
    storageToken: '${workload}p${token}'
    vaultId: primaryVaultResourceId
    authorizationModel: primaryVaultAuthorizationModel
    topicId: primarySystemTopicResourceId
    workerId: primaryWorkerIdentityResourceId
  }
  {
    prefix: secondaryPrefix
    groupName: secondaryGroup.name
    storageToken: '${workload}s${token}'
    vaultId: secondaryVaultResourceId
    authorizationModel: secondaryVaultAuthorizationModel
    topicId: secondarySystemTopicResourceId
    workerId: secondaryWorkerIdentityResourceId
  }
]

var stamps = [for stamp in stampInputs: {
  identityId: empty(stamp.workerId) ? resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.ManagedIdentity/userAssignedIdentities', 'id-${stamp.prefix}-worker') : stamp.workerId
  principalId: empty(stamp.workerId) ? '' : resolvedWorkerIdentities[toLower(stamp.workerId)].principalId
  vaultId: empty(stamp.vaultId) ? resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.KeyVault/vaults', 'kv-${stamp.prefix}') : stamp.vaultId
  vaultName: empty(stamp.vaultId) ? 'kv-${stamp.prefix}' : last(split(stamp.vaultId, '/'))
  vaultUri: empty(stamp.vaultId) ? 'https://kv-${stamp.prefix}${environment().suffixes.keyvaultDns}/' : resolvedExistingVaults[toLower(stamp.vaultId)].vaultUri
  vaultUsesRbac: empty(stamp.vaultId) ? stamp.authorizationModel == 'rbac' : resolvedExistingVaults[toLower(stamp.vaultId)].usesRbac
  vaultTenantId: empty(stamp.vaultId) ? subscription().tenantId : resolvedExistingVaults[toLower(stamp.vaultId)].tenantId
  topicName: empty(stamp.topicId) ? 'evgt-${stamp.prefix}' : last(split(stamp.topicId, '/'))
  queueId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.ServiceBus/namespaces/queues', 'sb-${stamp.prefix}', 'kv-events')
  storageId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Storage/storageAccounts', 'st${stamp.storageToken}run')
  deadletterStorageId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Storage/storageAccounts', 'st${stamp.storageToken}dl')
  monitorScopeId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Insights/privateLinkScopes', 'ampls-${stamp.prefix}')
  endpointSubnetId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Network/virtualNetworks/subnets', 'vnet-${stamp.prefix}', 'endpoints')
  zoneIds: toObject(items(zoneNames), zone => zone.key, zone => resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Network/privateDnsZones', zone.value))
  runtime: {
    identityId: empty(stamp.workerId) ? resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.ManagedIdentity/userAssignedIdentities', 'id-${stamp.prefix}-worker') : stamp.workerId
    storageName: 'st${stamp.storageToken}run'
    storageBlobUri: 'https://st${stamp.storageToken}run.blob.${environment().suffixes.storage}/'
    busNamespace: 'sb-${stamp.prefix}.servicebus.windows.net'
    integrationSubnetId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Network/virtualNetworks/subnets', 'vnet-${stamp.prefix}', 'integration')
    endpointSubnetId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Network/virtualNetworks/subnets', 'vnet-${stamp.prefix}', 'endpoints')
    appDnsZoneId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Network/privateDnsZones', zoneNames.app)
    workspaceId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.OperationalInsights/workspaces', 'log-${stamp.prefix}')
    insightsId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Insights/components', 'appi-${stamp.prefix}')
    actionGroupId: resourceId(subscription().subscriptionId, stamp.groupName, 'Microsoft.Insights/actionGroups', 'ag-${stamp.prefix}')
  }
}]

var replicaWriterRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', guid(subscription().id, workload, 'secret-replica-writer'))

module primaryIdentity './modules/worker-identity.bicep' = if (managePermissions && empty(primaryWorkerIdentityResourceId)) {
  name: 'primary-worker-identity'
  scope: primaryGroup
  params: {
    prefix: primaryPrefix
    location: primaryLocation
    tags: tags
  }
}

module secondaryIdentity './modules/worker-identity.bicep' = if (managePermissions && empty(secondaryWorkerIdentityResourceId)) {
  name: 'secondary-worker-identity'
  scope: secondaryGroup
  params: {
    prefix: secondaryPrefix
    location: secondaryLocation
    tags: tags
  }
}

module primary './modules/foundation.bicep' = {
  name: 'primary-foundation'
  scope: primaryGroup
  params: {
    prefix: primaryPrefix
    storageToken: '${workload}p${token}'
    location: primaryLocation
    network: primaryNetwork
    alertEmail: alertEmail
    tags: tags
    existingVaultResourceId: primaryVaultResourceId
    vaultAuthorizationModel: primaryVaultAuthorizationModel
    managePermissions: managePermissions
    configureOwnedVaultDiagnostics: resolvedOwnedFoundationDiagnostics.?primary.?vault ?? false
    configureOwnedTopicDiagnostics: resolvedOwnedFoundationDiagnostics.?primary.?topic ?? false
    workerIdentityResourceId: stamps[0].identityId
    workerPrincipalId: stamps[0].principalId
    replicaWriterPrincipalId: stamps[1].principalId
    replicaWriterIdentityResourceId: stamps[1].identityId
    existingSystemTopicResourceId: primarySystemTopicResourceId
    zoneNames: zoneNames
  }
  dependsOn: [primaryIdentity, secondaryIdentity]
}

module secondary './modules/foundation.bicep' = {
  name: 'secondary-foundation'
  scope: secondaryGroup
  params: {
    prefix: secondaryPrefix
    storageToken: '${workload}s${token}'
    location: secondaryLocation
    network: secondaryNetwork
    alertEmail: alertEmail
    tags: tags
    existingVaultResourceId: secondaryVaultResourceId
    vaultAuthorizationModel: secondaryVaultAuthorizationModel
    managePermissions: managePermissions
    configureOwnedVaultDiagnostics: resolvedOwnedFoundationDiagnostics.?secondary.?vault ?? false
    configureOwnedTopicDiagnostics: resolvedOwnedFoundationDiagnostics.?secondary.?topic ?? false
    workerIdentityResourceId: stamps[1].identityId
    workerPrincipalId: stamps[1].principalId
    replicaWriterPrincipalId: stamps[0].principalId
    replicaWriterIdentityResourceId: stamps[0].identityId
    existingSystemTopicResourceId: secondarySystemTopicResourceId
    zoneNames: zoneNames
  }
  dependsOn: [primaryIdentity, secondaryIdentity]
}

module replicaWriterRole './modules/replica-writer-role.bicep' = if (managePermissions && empty(replicaWriterRoleResourceId)) {
  name: '${workload}-${token}-replica-writer-role'
  params: {
    workload: workload
    token: token
    enabled: stamps[0].vaultUsesRbac || stamps[1].vaultUsesRbac
  }
}

module primaryVaultAccess './modules/vault-access.bicep' = if (managePermissions) {
  name: 'primary-vault-access'
  scope: resourceGroup(empty(primaryVaultResourceId) ? primaryGroup.name : split(primaryVaultResourceId, '/')[4])
  params: {
    vaultName: stamps[0].vaultName
    isExistingVault: !empty(primaryVaultResourceId)
    useRbac: stamps[0].vaultUsesRbac
    tenantId: stamps[0].vaultTenantId
    readerIdentityResourceId: stamps[0].identityId
    writerIdentityResourceId: stamps[1].identityId
    readerPrincipalId: stamps[0].principalId
    writerPrincipalId: stamps[1].principalId
    writerRoleId: empty(replicaWriterRoleResourceId) ? replicaWriterRoleId : replicaWriterRoleResourceId
    existingRoleAssignmentNames: resolvedVaultRoleAssignments
  }
  dependsOn: [primary, secondaryIdentity, replicaWriterRole]
}

module secondaryVaultAccess './modules/vault-access.bicep' = if (managePermissions) {
  name: 'secondary-vault-access'
  scope: resourceGroup(empty(secondaryVaultResourceId) ? secondaryGroup.name : split(secondaryVaultResourceId, '/')[4])
  params: {
    vaultName: stamps[1].vaultName
    isExistingVault: !empty(secondaryVaultResourceId)
    useRbac: stamps[1].vaultUsesRbac
    tenantId: stamps[1].vaultTenantId
    readerIdentityResourceId: stamps[1].identityId
    writerIdentityResourceId: stamps[0].identityId
    readerPrincipalId: stamps[1].principalId
    writerPrincipalId: stamps[0].principalId
    writerRoleId: empty(replicaWriterRoleResourceId) ? replicaWriterRoleId : replicaWriterRoleResourceId
    existingRoleAssignmentNames: resolvedVaultRoleAssignments
  }
  dependsOn: [secondary, primaryIdentity, replicaWriterRole]
}

module primaryEndpoints './modules/endpoints.bicep' = {
  name: 'primary-endpoints'
  scope: primaryGroup
  params: {
    prefix: primaryPrefix
    location: primaryLocation
    subnetId: stamps[0].endpointSubnetId
    zoneIds: stamps[0].zoneIds
    localVaultId: stamps[0].vaultId
    remoteVaultId: stamps[1].vaultId
    storageId: stamps[0].storageId
    deadletterStorageId: stamps[0].deadletterStorageId
    serviceBusId: resourceId(subscription().subscriptionId, primaryGroup.name, 'Microsoft.ServiceBus/namespaces', 'sb-${primaryPrefix}')
    monitorScopeId: stamps[0].monitorScopeId
    tags: tags
  }
  dependsOn: [primary, secondary]
}

module secondaryEndpoints './modules/endpoints.bicep' = {
  name: 'secondary-endpoints'
  scope: secondaryGroup
  params: {
    prefix: secondaryPrefix
    location: secondaryLocation
    subnetId: stamps[1].endpointSubnetId
    zoneIds: stamps[1].zoneIds
    localVaultId: stamps[1].vaultId
    remoteVaultId: stamps[0].vaultId
    storageId: stamps[1].storageId
    deadletterStorageId: stamps[1].deadletterStorageId
    serviceBusId: resourceId(subscription().subscriptionId, secondaryGroup.name, 'Microsoft.ServiceBus/namespaces', 'sb-${secondaryPrefix}')
    monitorScopeId: stamps[1].monitorScopeId
    tags: tags
  }
  dependsOn: [primary, secondary]
}

module primaryApp './modules/function-app.bicep' = {
  name: 'primary-worker'
  scope: primaryGroup
  params: {
    prefix: primaryPrefix
    location: primaryLocation
    foundation: stamps[0].runtime
    sourceVaultId: stamps[0].vaultId
    sourceVaultUri: stamps[0].vaultUri
    destinationVaultUri: stamps[1].vaultUri
    destinationVaultId: stamps[1].vaultId
    secretNames: secretNames
    secretPrefix: secretPrefix
    replicateAllSecrets: replicateAllSecrets
    allowUnmanagedOverwrite: allowUnmanagedOverwrite
    enabled: enableWorker && authoritativeRegion == 'primary'
    tags: tags
  }
  dependsOn: [
    primaryEndpoints
    primaryVaultAccess
    secondaryVaultAccess
  ]
}

module secondaryApp './modules/function-app.bicep' = {
  name: 'secondary-worker'
  scope: secondaryGroup
  params: {
    prefix: secondaryPrefix
    location: secondaryLocation
    foundation: stamps[1].runtime
    sourceVaultId: stamps[1].vaultId
    sourceVaultUri: stamps[1].vaultUri
    destinationVaultUri: stamps[0].vaultUri
    destinationVaultId: stamps[0].vaultId
    secretNames: secretNames
    secretPrefix: secretPrefix
    replicateAllSecrets: replicateAllSecrets
    allowUnmanagedOverwrite: allowUnmanagedOverwrite
    enabled: enableWorker && authoritativeRegion == 'secondary'
    tags: tags
  }
  dependsOn: [
    secondaryEndpoints
    primaryVaultAccess
    secondaryVaultAccess
  ]
}

module primaryRouting './modules/event-routing.bicep' = if (deployEventRouting) {
  name: 'primary-routing'
  scope: resourceGroup(empty(primarySystemTopicResourceId) ? primaryGroup.name : split(primarySystemTopicResourceId, '/')[4])
  params: {
    topicName: stamps[0].topicName
    queueId: stamps[0].queueId
    deadletterStorageId: stamps[0].deadletterStorageId
    enabled: enableEventRouting && authoritativeRegion == 'primary'
    subscriptionName: empty(primarySystemTopicResourceId) ? 'secret-events' : '${workload}-${token}-secret-events'
  }
  dependsOn: [
    primaryApp
  ]
}

module secondaryRouting './modules/event-routing.bicep' = if (deployEventRouting) {
  name: 'secondary-routing'
  scope: resourceGroup(empty(secondarySystemTopicResourceId) ? secondaryGroup.name : split(secondarySystemTopicResourceId, '/')[4])
  params: {
    topicName: stamps[1].topicName
    queueId: stamps[1].queueId
    deadletterStorageId: stamps[1].deadletterStorageId
    enabled: enableEventRouting && authoritativeRegion == 'secondary'
    subscriptionName: empty(secondarySystemTopicResourceId) ? 'secret-events' : '${workload}-${token}-secret-events'
  }
  dependsOn: [
    secondaryApp
  ]
}

output primaryResources object = {
  resourceGroup: primaryGroup.name
  functionApp: 'func-${primaryPrefix}'
  vault: stamps[0].vaultName
  vaultUri: stamps[0].vaultUri
  vaultResourceId: stamps[0].vaultId
  vaultResourceGroup: empty(primaryVaultResourceId) ? primaryGroup.name : split(primaryVaultResourceId, '/')[4]
  vaultIsExisting: !empty(primaryVaultResourceId)
  vaultAuthorizationModel: stamps[0].vaultUsesRbac ? 'rbac' : 'accessPolicy'
  storage: stamps[0].runtime.storageName
  serviceBus: 'sb-${primaryPrefix}'
  identityResourceId: stamps[0].identityId
  identityClientId: empty(primaryWorkerIdentityResourceId) ? primaryIdentity!.outputs.clientId : resolvedWorkerIdentities[toLower(primaryWorkerIdentityResourceId)].clientId
  identityPrincipalId: empty(primaryWorkerIdentityResourceId) ? primaryIdentity!.outputs.principalId : stamps[0].principalId
}

output secondaryResources object = {
  resourceGroup: secondaryGroup.name
  functionApp: 'func-${secondaryPrefix}'
  vault: stamps[1].vaultName
  vaultUri: stamps[1].vaultUri
  vaultResourceId: stamps[1].vaultId
  vaultResourceGroup: empty(secondaryVaultResourceId) ? secondaryGroup.name : split(secondaryVaultResourceId, '/')[4]
  vaultIsExisting: !empty(secondaryVaultResourceId)
  vaultAuthorizationModel: stamps[1].vaultUsesRbac ? 'rbac' : 'accessPolicy'
  storage: stamps[1].runtime.storageName
  serviceBus: 'sb-${secondaryPrefix}'
  identityResourceId: stamps[1].identityId
  identityClientId: empty(secondaryWorkerIdentityResourceId) ? secondaryIdentity!.outputs.clientId : resolvedWorkerIdentities[toLower(secondaryWorkerIdentityResourceId)].clientId
  identityPrincipalId: empty(secondaryWorkerIdentityResourceId) ? secondaryIdentity!.outputs.principalId : stamps[1].principalId
}
