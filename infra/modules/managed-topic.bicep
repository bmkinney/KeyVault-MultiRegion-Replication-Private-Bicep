param prefix string
param location string
param tags object
param vaultResourceId string

resource topic 'Microsoft.EventGrid/systemTopics@2025-02-15' = {
  name: 'evgt-${prefix}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    source: vaultResourceId
    topicType: 'Microsoft.KeyVault.vaults'
  }
}
