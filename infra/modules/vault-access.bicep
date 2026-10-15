@description('Existing vault name.')
param vaultName string
@description('Actual authorization model, discovered from reused vaults without changing it.')
param useRbac bool
@description('Tenant ID of the selected vault.')
param tenantId string
@description('Local source-reader identity principal ID.')
param readerPrincipalId string
@description('Opposite-region replica-writer identity principal ID.')
param writerPrincipalId string
@description('Custom replica-writer role definition ID, excluding destructive secret operations.')
param writerRoleId string

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: vaultName
}

var assignments = [
  {
    principal: readerPrincipalId
    role: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
  {
    principal: writerPrincipalId
    role: writerRoleId
  }
]

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignment in assignments: if (useRbac) {
  name: guid(vault.id, assignment.principal, assignment.role)
  scope: vault
  properties: {
    principalId: assignment.principal
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
        objectId: readerPrincipalId
        permissions: {
          secrets: ['get', 'list']
        }
      }
      {
        tenantId: tenantId
        objectId: writerPrincipalId
        permissions: {
          secrets: ['get', 'list', 'set']
        }
      }
    ]
  }
}
