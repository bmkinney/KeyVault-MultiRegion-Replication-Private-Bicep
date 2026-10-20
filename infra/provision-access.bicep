param prefix string
param storageToken string
param workerIdentityResourceId string
param workerPrincipalId string
param systemTopicResourceId string
param topicPrincipalId string
param vaultResourceId string
param isExistingVault bool
param useRbac bool
param tenantId string
param writerIdentityResourceId string
param writerPrincipalId string
param writerRoleId string
param existingRoleAssignmentNames object = {}

module services './modules/service-access.bicep' = {
  name: 'service-access'
  params: {
    prefix: prefix
    storageToken: storageToken
    workerIdentityResourceId: workerIdentityResourceId
    workerPrincipalId: workerPrincipalId
    systemTopicResourceId: systemTopicResourceId
    topicPrincipalId: topicPrincipalId
  }
}
module vault './modules/vault-access.bicep' = {
  name: 'vault-access'
  scope: resourceGroup(split(vaultResourceId, '/')[4])
  params: {
    vaultName: last(split(vaultResourceId, '/'))
    isExistingVault: isExistingVault
    useRbac: useRbac
    tenantId: tenantId
    readerIdentityResourceId: workerIdentityResourceId
    readerPrincipalId: workerPrincipalId
    writerIdentityResourceId: writerIdentityResourceId
    writerPrincipalId: writerPrincipalId
    writerRoleId: writerRoleId
    existingRoleAssignmentNames: existingRoleAssignmentNames
  }
}
