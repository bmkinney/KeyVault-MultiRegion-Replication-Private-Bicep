param prefix string
param location string
param tags object
@allowed(['accessPolicy', 'rbac'])
param vaultAuthorizationModel string
param workerIdentityResourceId string
param replicaWriterIdentityResourceId string
param workerPrincipalId string = ''
param replicaWriterPrincipalId string = ''

resource reader 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(workerIdentityResourceId, '/'))
  scope: resourceGroup(split(workerIdentityResourceId, '/')[4])
}
resource writer 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(replicaWriterIdentityResourceId, '/'))
  scope: resourceGroup(split(replicaWriterIdentityResourceId, '/')[4])
}

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' = {
  name: 'kv-${prefix}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: vaultAuthorizationModel == 'rbac'
    accessPolicies: vaultAuthorizationModel == 'rbac' ? [] : [
      {
        tenantId: subscription().tenantId
        objectId: empty(workerPrincipalId) ? reader.properties.principalId : workerPrincipalId
        permissions: { secrets: ['get', 'list'] }
      }
      {
        tenantId: subscription().tenantId
        objectId: empty(replicaWriterPrincipalId) ? writer.properties.principalId : replicaWriterPrincipalId
        permissions: { secrets: ['get', 'list', 'set'] }
      }
    ]
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Disabled'
    networkAcls: { bypass: 'None', defaultAction: 'Deny' }
  }
}
