targetScope = 'subscription'

param workload string
param token string
@description('Define the role only when at least one selected vault uses RBAC.')
param enabled bool

resource role 'Microsoft.Authorization/roleDefinitions@2022-04-01' = if (enabled) {
  name: guid(subscription().id, workload, 'secret-replica-writer')
  properties: {
    roleName: '${workload} Secret Replica Writer ${token}'
    description: 'Read and set current replica secrets and attributes; no delete, purge, backup, or restore.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.KeyVault/vaults/secrets/readMetadata/action'
          'Microsoft.KeyVault/vaults/secrets/getSecret/action'
          'Microsoft.KeyVault/vaults/secrets/setSecret/action'
          'Microsoft.KeyVault/vaults/secrets/update/action'
        ]
        notDataActions: []
      }
    ]
  }
}

output id string = enabled ? role!.id : ''
