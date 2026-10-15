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

@description('Authorization for a solution-managed primary vault. Reused vaults retain their detected model.')
@allowed(['accessPolicy', 'rbac'])
param primaryVaultAuthorizationModel string = 'accessPolicy'

@description('Authorization for a solution-managed secondary vault. Reused vaults retain their detected model.')
@allowed(['accessPolicy', 'rbac'])
param secondaryVaultAuthorizationModel string = 'accessPolicy'

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

module primaryIdentity './modules/worker-identity.bicep' = {
  name: 'primary-worker-identity'
  scope: primaryGroup
  params: {
    prefix: primaryPrefix
    location: primaryLocation
    tags: tags
  }
}

module secondaryIdentity './modules/worker-identity.bicep' = {
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
    replicaWriterPrincipalId: secondaryIdentity.outputs.principalId
    existingSystemTopicResourceId: primarySystemTopicResourceId
  }
  dependsOn: [primaryIdentity]
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
    replicaWriterPrincipalId: primaryIdentity.outputs.principalId
    existingSystemTopicResourceId: secondarySystemTopicResourceId
  }
  dependsOn: [secondaryIdentity]
}

module replicaWriterRole './modules/replica-writer-role.bicep' = {
  name: '${workload}-${token}-replica-writer-role'
  params: {
    workload: workload
    token: token
    enabled: primary.outputs.vaultUsesRbac || secondary.outputs.vaultUsesRbac
  }
}

module primaryVaultAccess './modules/vault-access.bicep' = {
  name: 'primary-vault-access'
  scope: resourceGroup(empty(primaryVaultResourceId) ? primaryGroup.name : split(primaryVaultResourceId, '/')[4])
  params: {
    vaultName: primary.outputs.vaultName
    useRbac: primary.outputs.vaultUsesRbac
    tenantId: primary.outputs.vaultTenantId
    readerPrincipalId: primary.outputs.principalId
    writerPrincipalId: secondary.outputs.principalId
    writerRoleId: replicaWriterRole.outputs.id
  }
}

module secondaryVaultAccess './modules/vault-access.bicep' = {
  name: 'secondary-vault-access'
  scope: resourceGroup(empty(secondaryVaultResourceId) ? secondaryGroup.name : split(secondaryVaultResourceId, '/')[4])
  params: {
    vaultName: secondary.outputs.vaultName
    useRbac: secondary.outputs.vaultUsesRbac
    tenantId: secondary.outputs.vaultTenantId
    readerPrincipalId: secondary.outputs.principalId
    writerPrincipalId: primary.outputs.principalId
    writerRoleId: replicaWriterRole.outputs.id
  }
}

module primaryEndpoints './modules/endpoints.bicep' = {
  name: 'primary-endpoints'
  scope: primaryGroup
  params: {
    prefix: primaryPrefix
    location: primaryLocation
    subnetId: primary.outputs.endpointSubnetId
    zoneIds: primary.outputs.zoneIds
    localVaultId: primary.outputs.vaultId
    remoteVaultId: secondary.outputs.vaultId
    storageId: primary.outputs.storageId
    deadletterStorageId: primary.outputs.deadletterStorageId
    serviceBusId: primary.outputs.serviceBusId
    monitorScopeId: primary.outputs.monitorScopeId
    tags: tags
  }
}

module secondaryEndpoints './modules/endpoints.bicep' = {
  name: 'secondary-endpoints'
  scope: secondaryGroup
  params: {
    prefix: secondaryPrefix
    location: secondaryLocation
    subnetId: secondary.outputs.endpointSubnetId
    zoneIds: secondary.outputs.zoneIds
    localVaultId: secondary.outputs.vaultId
    remoteVaultId: primary.outputs.vaultId
    storageId: secondary.outputs.storageId
    deadletterStorageId: secondary.outputs.deadletterStorageId
    serviceBusId: secondary.outputs.serviceBusId
    monitorScopeId: secondary.outputs.monitorScopeId
    tags: tags
  }
}

module primaryApp './modules/function-app.bicep' = {
  name: 'primary-worker'
  scope: primaryGroup
  params: {
    prefix: primaryPrefix
    location: primaryLocation
    foundation: primary.outputs.runtime
    sourceVaultId: primary.outputs.vaultId
    sourceVaultUri: primary.outputs.vaultUri
    destinationVaultUri: secondary.outputs.vaultUri
    destinationVaultId: secondary.outputs.vaultId
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
    foundation: secondary.outputs.runtime
    sourceVaultId: secondary.outputs.vaultId
    sourceVaultUri: secondary.outputs.vaultUri
    destinationVaultUri: primary.outputs.vaultUri
    destinationVaultId: primary.outputs.vaultId
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

module primaryRouting './modules/event-routing.bicep' = {
  name: 'primary-routing'
  scope: resourceGroup(empty(primarySystemTopicResourceId) ? primaryGroup.name : split(primarySystemTopicResourceId, '/')[4])
  params: {
    topicName: primary.outputs.topicName
    queueId: primary.outputs.queueId
    deadletterStorageId: primary.outputs.deadletterStorageId
    enabled: enableEventRouting && authoritativeRegion == 'primary'
    subscriptionName: empty(primarySystemTopicResourceId) ? 'secret-events' : '${workload}-${token}-secret-events'
  }
  dependsOn: [
    primaryApp
  ]
}

module secondaryRouting './modules/event-routing.bicep' = {
  name: 'secondary-routing'
  scope: resourceGroup(empty(secondarySystemTopicResourceId) ? secondaryGroup.name : split(secondarySystemTopicResourceId, '/')[4])
  params: {
    topicName: secondary.outputs.topicName
    queueId: secondary.outputs.queueId
    deadletterStorageId: secondary.outputs.deadletterStorageId
    enabled: enableEventRouting && authoritativeRegion == 'secondary'
    subscriptionName: empty(secondarySystemTopicResourceId) ? 'secret-events' : '${workload}-${token}-secret-events'
  }
  dependsOn: [
    secondaryApp
  ]
}

output primaryResources object = {
  resourceGroup: primaryGroup.name
  functionApp: primaryApp.outputs.name
  vault: primary.outputs.vaultName
  vaultUri: primary.outputs.vaultUri
  vaultResourceId: primary.outputs.vaultId
  vaultResourceGroup: primary.outputs.vaultResourceGroup
  vaultIsExisting: !empty(primaryVaultResourceId)
  vaultAuthorizationModel: primary.outputs.vaultUsesRbac ? 'rbac' : 'accessPolicy'
  storage: primary.outputs.storageName
  serviceBus: primary.outputs.serviceBusName
  identityClientId: primary.outputs.clientId
  identityPrincipalId: primary.outputs.principalId
}

output secondaryResources object = {
  resourceGroup: secondaryGroup.name
  functionApp: secondaryApp.outputs.name
  vault: secondary.outputs.vaultName
  vaultUri: secondary.outputs.vaultUri
  vaultResourceId: secondary.outputs.vaultId
  vaultResourceGroup: secondary.outputs.vaultResourceGroup
  vaultIsExisting: !empty(secondaryVaultResourceId)
  vaultAuthorizationModel: secondary.outputs.vaultUsesRbac ? 'rbac' : 'accessPolicy'
  storage: secondary.outputs.storageName
  serviceBus: secondary.outputs.serviceBusName
  identityClientId: secondary.outputs.clientId
  identityPrincipalId: secondary.outputs.principalId
}
