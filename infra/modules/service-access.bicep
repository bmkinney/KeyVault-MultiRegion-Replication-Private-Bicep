param prefix string
param storageToken string
param workerIdentityResourceId string
param workerPrincipalId string = ''
param systemTopicResourceId string
param topicPrincipalId string = ''

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(workerIdentityResourceId, '/'))
  scope: resourceGroup(split(workerIdentityResourceId, '/')[2], split(workerIdentityResourceId, '/')[4])
}
resource topic 'Microsoft.EventGrid/systemTopics@2025-02-15' existing = {
  name: last(split(systemTopicResourceId, '/'))
  scope: resourceGroup(split(systemTopicResourceId, '/')[4])
}
var worker = empty(workerPrincipalId) ? identity.properties.principalId : workerPrincipalId
var publisher = empty(topicPrincipalId) ? topic.identity.principalId : topicPrincipalId

resource runtime 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: 'st${storageToken}run'
}
resource deadletter 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: 'st${storageToken}dl'
}
resource bus 'Microsoft.ServiceBus/namespaces@2024-01-01' existing = {
  name: 'sb-${prefix}'
}
resource queue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  parent: bus
  name: 'kv-events'
}
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-${prefix}'
}

var storageRoles = [
  'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
]
resource hostAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in storageRoles: {
  name: guid(runtime.id, identity.id, role)
  scope: runtime
  properties: {
    principalId: worker
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]
resource receiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(queue.id, identity.id, 'receiver')
  scope: queue
  properties: {
    principalId: worker
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0')
  }
}
resource sender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(bus.id, topic.id, 'sender')
  scope: bus
  properties: {
    principalId: publisher
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39')
  }
}
resource deadletterWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(deadletter.id, topic.id, 'blob-writer')
  scope: deadletter
  properties: {
    principalId: publisher
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}
resource telemetryAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, identity.id, 'metrics-publisher')
  scope: insights
  properties: {
    principalId: worker
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}
