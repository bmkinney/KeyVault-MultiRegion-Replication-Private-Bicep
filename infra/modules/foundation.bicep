@description('Regional naming prefix.')
param prefix string
@description('Alphanumeric storage name token.')
param storageToken string
@description('Regional location.')
param location string
@description('VNet and subnet CIDRs.')
param network object
@description('Optional alert notification email.')
param alertEmail string
@description('Resource tags.')
param tags object
@description('Same-subscription existing vault ARM ID; empty creates a new vault.')
param existingVaultResourceId string = ''
@description('Authorization for a solution-managed vault; ignored for a reused vault.')
@allowed(['accessPolicy', 'rbac'])
param vaultAuthorizationModel string = 'accessPolicy'
@description('Opposite-region worker identity ARM ID, known before deployment.')
param replicaWriterIdentityResourceId string
@description('Shared regional private DNS zone names, supplied by the entrypoint.')
param zoneNames object
@description('Existing system topic ARM ID; empty creates a dedicated topic if none exists.')
param existingSystemTopicResourceId string = ''

resource vnet 'Microsoft.Network/virtualNetworks@2025-05-01' = {
  name: 'vnet-${prefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [network.address]
    }
    subnets: [
      {
        name: 'integration'
        properties: {
          addressPrefix: network.integration
          delegations: [
            {
              name: 'flex'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'endpoints'
        properties: {
          addressPrefix: network.endpoints
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

var zoneList = items(zoneNames)

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zone in zoneList: {
  name: zone.value
  location: 'global'
  tags: tags
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zone, index) in zoneList: {
  parent: zones[index]
  name: 'local-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}]

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: 'id-${prefix}-worker'
}

resource replicaWriterIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(replicaWriterIdentityResourceId, '/'))
  scope: resourceGroup(split(replicaWriterIdentityResourceId, '/')[4])
}

var workerPolicies = [
  {
    tenantId: subscription().tenantId
    objectId: identity.properties.principalId
    permissions: {
      secrets: ['get', 'list']
    }
  }
  {
    tenantId: subscription().tenantId
    objectId: replicaWriterIdentity.properties.principalId
    permissions: {
      secrets: ['get', 'list', 'set']
    }
  }
]

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' = if (empty(existingVaultResourceId)) {
  name: 'kv-${prefix}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: vaultAuthorizationModel == 'rbac'
    accessPolicies: vaultAuthorizationModel == 'rbac' ? [] : workerPolicies
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
    }
  }
}

resource referencedVault 'Microsoft.KeyVault/vaults@2026-02-01' existing = if (!empty(existingVaultResourceId)) {
  name: last(split(existingVaultResourceId, '/'))
  scope: resourceGroup(empty(existingVaultResourceId) ? resourceGroup().name : split(existingVaultResourceId, '/')[4])
}

var selectedVaultId = empty(existingVaultResourceId) ? vault!.id : referencedVault!.id
var selectedVaultUri = empty(existingVaultResourceId) ? vault!.properties.vaultUri : referencedVault!.properties.vaultUri

var accountNames = ['st${storageToken}run', 'st${storageToken}dl']
resource accounts 'Microsoft.Storage/storageAccounts@2025-06-01' = [for (accountName, index) in accountNames: {
  name: accountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_ZRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: index == 1 ? 'AzureServices' : 'None'
    }
  }
}]

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = [for (accountName, index) in accountNames: {
  parent: accounts[index]
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}]

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobs[0]
  name: 'deployment'
  properties: {
    publicAccess: 'None'
  }
}

resource coordinationContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobs[0]
  name: 'coordination'
  properties: {
    publicAccess: 'None'
  }
}

resource deadletterContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobs[1]
  name: 'eventgrid-deadletter'
  properties: {
    publicAccess: 'None'
  }
}

var storageRoles = [
  'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' // Functions host: Blob Data Owner.
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3' // Host diagnostic events: Table Data Contributor.
]
resource hostAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in storageRoles: {
  name: guid(accounts[0].id, identity.id, role)
  scope: accounts[0]
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]

resource bus 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: 'sb-${prefix}'
  location: location
  tags: tags
  sku: {
    name: 'Premium'
    tier: 'Premium'
    capacity: 1
  }
  properties: {
    premiumMessagingPartitions: 1
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    zoneRedundant: true
  }
}

resource firewall 'Microsoft.ServiceBus/namespaces/networkRuleSets@2024-01-01' = {
  parent: bus
  name: 'default'
  properties: {
    publicNetworkAccess: 'Disabled'
    defaultAction: 'Deny'
    trustedServiceAccessEnabled: true
    ipRules: []
    virtualNetworkRules: []
  }
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: bus
  name: 'kv-events'
  properties: {
    lockDuration: 'PT1M'
    maxDeliveryCount: 10
    defaultMessageTimeToLive: 'P14D'
    deadLetteringOnMessageExpiration: true
    enablePartitioning: false
    requiresSession: false
  }
}

resource receiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(queue.id, identity.id, 'receiver')
  scope: queue
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0')
  }
}

resource topic 'Microsoft.EventGrid/systemTopics@2025-02-15' = if (empty(existingSystemTopicResourceId)) {
  name: 'evgt-${prefix}'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    source: selectedVaultId
    topicType: 'Microsoft.KeyVault.vaults'
  }
}

resource referencedTopic 'Microsoft.EventGrid/systemTopics@2025-02-15' existing = if (!empty(existingSystemTopicResourceId)) {
  name: last(split(existingSystemTopicResourceId, '/'))
  scope: resourceGroup(empty(existingSystemTopicResourceId) ? resourceGroup().name : split(existingSystemTopicResourceId, '/')[4])
}

var selectedTopicId = empty(existingSystemTopicResourceId) ? topic!.id : referencedTopic!.id
var selectedTopicPrincipal = empty(existingSystemTopicResourceId) ? topic!.identity.principalId : referencedTopic!.identity.principalId

resource sender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(bus.id, selectedTopicId, 'sender')
  scope: bus
  properties: {
    principalId: selectedTopicPrincipal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39')
  }
}

resource deadletterWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(accounts[1].id, selectedTopicId, 'blob-writer')
  scope: accounts[1]
  properties: {
    principalId: selectedTopicPrincipal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: 'log-${prefix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
    features: {
      disableLocalAuth: true
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
  }
}

resource telemetryAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, identity.id, 'metrics-publisher')
  scope: insights
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}

resource monitor 'Microsoft.Insights/privateLinkScopes@2023-06-01-preview' = {
  name: 'ampls-${prefix}'
  location: 'global'
  tags: tags
  properties: {
    accessModeSettings: {
      ingestionAccessMode: 'PrivateOnly'
      queryAccessMode: 'PrivateOnly'
    }
  }
}

resource workspaceLink 'Microsoft.Insights/privateLinkScopes/scopedResources@2023-06-01-preview' = {
  parent: monitor
  name: 'workspace'
  properties: {
    kind: 'Resource'
    linkedResourceId: workspace.id
  }
}

resource insightsLink 'Microsoft.Insights/privateLinkScopes/scopedResources@2023-06-01-preview' = {
  parent: monitor
  name: 'insights'
  properties: {
    kind: 'Resource'
    linkedResourceId: insights.id
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${prefix}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'mrkv'
    enabled: true
    emailReceivers: empty(alertEmail) ? [] : [
      {
        name: 'operator'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

resource dlqAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${prefix}-dlq'
  location: 'global'
  tags: tags
  properties: {
    description: 'Any dead-letter message requires investigation; never automatically purge or replay.'
    severity: 1
    enabled: true
    scopes: [bus.id]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'deadletter'
          metricNamespace: 'Microsoft.ServiceBus/namespaces'
          metricName: 'DeadletteredMessages'
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Maximum'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

resource eventFailureAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${prefix}-event-delivery'
  location: 'global'
  tags: tags
  properties: {
    description: 'Event Grid could not deliver events to Service Bus.'
    severity: 1
    enabled: true
    scopes: [selectedTopicId]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'delivery'
          metricNamespace: 'Microsoft.EventGrid/systemTopics'
          metricName: 'DeliveryAttemptFailCount'
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [{ actionGroupId: actionGroup.id }]
  }
}

resource vaultDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (empty(existingVaultResourceId)) {
  name: 'workspace'
  scope: vault
  properties: {
    workspaceId: workspace.id
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

resource busDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'workspace'
  scope: bus
  properties: {
    workspaceId: workspace.id
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

resource topicDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (empty(existingSystemTopicResourceId)) {
  name: 'workspace'
  scope: topic
  properties: {
    workspaceId: workspace.id
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
  }
}

resource blobDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = [for (accountName, index) in accountNames: {
  name: 'workspace'
  scope: blobs[index]
  properties: {
    workspaceId: workspace.id
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'Transaction', enabled: true }]
  }
}]

output vaultId string = selectedVaultId
output vaultName string = empty(existingVaultResourceId) ? vault!.name : referencedVault!.name
output vaultUri string = selectedVaultUri
output vaultResourceGroup string = empty(existingVaultResourceId) ? resourceGroup().name : split(existingVaultResourceId, '/')[4]
output vaultUsesRbac bool = empty(existingVaultResourceId) ? vaultAuthorizationModel == 'rbac' : (referencedVault!.properties.?enableRbacAuthorization ?? false)
output vaultTenantId string = empty(existingVaultResourceId) ? subscription().tenantId : referencedVault!.properties.tenantId
output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
output storageId string = accounts[0].id
output storageName string = accounts[0].name
output deadletterStorageId string = accounts[1].id
output serviceBusId string = bus.id
output serviceBusName string = bus.name
output queueId string = queue.id
output topicName string = empty(existingSystemTopicResourceId) ? topic!.name : referencedTopic!.name
output topicResourceGroup string = empty(existingSystemTopicResourceId) ? resourceGroup().name : split(existingSystemTopicResourceId, '/')[4]
output monitorScopeId string = monitor.id
output endpointSubnetId string = '${vnet.id}/subnets/endpoints'
output zoneIds object = toObject(zoneList, zone => zone.key, zone => resourceId('Microsoft.Network/privateDnsZones', zone.value))
output runtime object = {
  identityId: identity.id
  identityClientId: identity.properties.clientId
  storageName: accounts[0].name
  storageBlobUri: accounts[0].properties.primaryEndpoints.blob
  busNamespace: '${bus.name}.servicebus.windows.net'
  integrationSubnetId: '${vnet.id}/subnets/integration'
  endpointSubnetId: '${vnet.id}/subnets/endpoints'
  appDnsZoneId: resourceId('Microsoft.Network/privateDnsZones', zoneNames.app)
  workspaceId: workspace.id
  insightsConnectionString: insights.properties.ConnectionString
  actionGroupId: actionGroup.id
}
