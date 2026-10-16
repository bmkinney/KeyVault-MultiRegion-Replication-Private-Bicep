@description('Existing vault name.')
param vaultName string
@description('Actual authorization model, discovered from reused vaults without changing it.')
param useRbac bool
@description('Tenant ID of the selected vault.')
param tenantId string
@description('Local source-reader identity ARM ID.')
param readerIdentityResourceId string
@description('Opposite-region replica-writer identity ARM ID.')
param writerIdentityResourceId string
@description('Custom replica-writer role definition ID, excluding destructive secret operations.')
param writerRoleId string
@description('Previously deployed assignment names discovered by the deployment script.')
param existingRoleAssignmentNames object = {}

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: vaultName
}

resource readerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(readerIdentityResourceId, '/'))
  scope: resourceGroup(split(readerIdentityResourceId, '/')[4])
}

resource writerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(writerIdentityResourceId, '/'))
  scope: resourceGroup(split(writerIdentityResourceId, '/')[4])
}

var readerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var assignments = [
  {
    identityId: readerIdentityResourceId
    role: readerRoleId
  }
  {
    identityId: writerIdentityResourceId
    role: writerRoleId
  }
]

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignment in assignments: if (useRbac) {
  name: contains(existingRoleAssignmentNames, toLower('${vault.id}|${assignment.identityId}|${assignment.role}'))
    ? existingRoleAssignmentNames[toLower('${vault.id}|${assignment.identityId}|${assignment.role}')]
    : guid(vault.id, assignment.identityId, assignment.role)
  scope: vault
  properties: {
    principalId: assignment.identityId == readerIdentityResourceId ? readerIdentity.properties.principalId : writerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: assignment.role
  }
}]

resource policies 'Microsoft.KeyVault/vaults/accessPolicies@2026-02-01' = if (!useRbac) {
  parent: vault
  name: 'add'
  properties: {
    accessPolicies: [
      {
        tenantId: tenantId
        objectId: readerIdentity.properties.principalId
        permissions: {
          secrets: ['get', 'list']
        }
      }
      {
        tenantId: tenantId
        objectId: writerIdentity.properties.principalId
        permissions: {
          secrets: ['get', 'list', 'set']
        }
      }
    ]
  }
}
