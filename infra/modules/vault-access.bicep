@description('Selected vault name.')
param vaultName string
@description('True only for vaults selected by resource ID. Managed vaults already include their complete policies in the foundation PUT.')
param isExistingVault bool
@description('Actual authorization model, discovered from reused vaults without changing it.')
param useRbac bool
@description('Tenant ID of the selected vault.')
param tenantId string
@description('Local source-reader identity ARM ID.')
param readerIdentityResourceId string
@description('Opposite-region replica-writer identity ARM ID.')
param writerIdentityResourceId string
@description('Read-only-discovered existing reader principal ID; empty resolves a newly created identity inside resource properties.')
param readerPrincipalId string = ''
@description('Read-only-discovered existing writer principal ID.')
param writerPrincipalId string = ''
@description('Custom replica-writer role definition ID, excluding destructive secret operations.')
param writerRoleId string
@description('Previously deployed assignment names discovered by the deployment script.')
param existingRoleAssignmentNames object = {}

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: vaultName
}

resource readerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(readerIdentityResourceId, '/'))
  scope: resourceGroup(split(readerIdentityResourceId, '/')[2], split(readerIdentityResourceId, '/')[4])
}

resource writerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(writerIdentityResourceId, '/'))
  scope: resourceGroup(split(writerIdentityResourceId, '/')[2], split(writerIdentityResourceId, '/')[4])
}

var readerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var selectedReaderPrincipalId = empty(readerPrincipalId) ? readerIdentity.properties.principalId : readerPrincipalId
var selectedWriterPrincipalId = empty(writerPrincipalId) ? writerIdentity.properties.principalId : writerPrincipalId
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
    principalId: assignment.identityId == readerIdentityResourceId ? selectedReaderPrincipalId : selectedWriterPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: assignment.role
  }
}]

resource policies 'Microsoft.KeyVault/vaults/accessPolicies@2026-02-01' = if (isExistingVault && !useRbac) {
  parent: vault
  name: 'add'
  properties: {
    accessPolicies: [
      {
        tenantId: tenantId
        objectId: selectedReaderPrincipalId
        permissions: {
          secrets: ['get', 'list']
        }
      }
      {
        tenantId: tenantId
        objectId: selectedWriterPrincipalId
        permissions: {
          secrets: ['get', 'list', 'set']
        }
      }
    ]
  }
}
