param prefix string
param location string
param tags object
param vaultResourceId string
param createVault bool
param createTopic bool
@allowed(['accessPolicy', 'rbac'])
param vaultAuthorizationModel string
param workerIdentityResourceId string
param replicaWriterIdentityResourceId string
param workerPrincipalId string
param replicaWriterPrincipalId string

module vault './modules/managed-vault.bicep' = if (createVault) {
  name: 'managed-vault'
  params: {
    prefix: prefix
    location: location
    tags: tags
    vaultAuthorizationModel: vaultAuthorizationModel
    workerIdentityResourceId: workerIdentityResourceId
    replicaWriterIdentityResourceId: replicaWriterIdentityResourceId
    workerPrincipalId: workerPrincipalId
    replicaWriterPrincipalId: replicaWriterPrincipalId
  }
}
module topic './modules/managed-topic.bicep' = if (createTopic) {
  name: 'managed-topic'
  params: {
    prefix: prefix
    location: location
    tags: tags
    vaultResourceId: vaultResourceId
  }
  dependsOn: [vault]
}
