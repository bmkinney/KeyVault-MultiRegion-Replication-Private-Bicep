@description('Regional naming prefix.')
param prefix string
@description('VNet region.')
param location string
@description('Private endpoint subnet ID.')
param subnetId string
@description('DNS zone IDs indexed by service.')
param zoneIds object
@description('Local vault ID.')
param localVaultId string
@description('Other-region vault ID.')
param remoteVaultId string
@description('Runtime storage account ID.')
param storageId string
@description('Dead-letter storage account ID.')
param deadletterStorageId string
@description('Local Service Bus namespace ID.')
param serviceBusId string
@description('Regional AMPLS ID.')
param monitorScopeId string
@description('Resource tags.')
param tags object

var targets = [
  { suffix: 'vault-local', id: localVaultId, group: 'vault', zones: [zoneIds.vault] }
  { suffix: 'vault-remote', id: remoteVaultId, group: 'vault', zones: [zoneIds.vault] }
  { suffix: 'bus', id: serviceBusId, group: 'namespace', zones: [zoneIds.bus] }
  { suffix: 'blob', id: storageId, group: 'blob', zones: [zoneIds.blob] }
  { suffix: 'queue', id: storageId, group: 'queue', zones: [zoneIds.queue] }
  { suffix: 'table', id: storageId, group: 'table', zones: [zoneIds.table] }
  { suffix: 'deadletter', id: deadletterStorageId, group: 'blob', zones: [zoneIds.blob] }
  {
    suffix: 'monitor'
    id: monitorScopeId
    group: 'azuremonitor'
    zones: [zoneIds.monitor, zoneIds.oms, zoneIds.ods, zoneIds.agent, zoneIds.blob]
  }
]

module endpoints './private-endpoint.bicep' = [for target in targets: {
  name: 'endpoint-${target.suffix}'
  params: {
    name: 'pe-${prefix}-${target.suffix}'
    location: location
    subnetId: subnetId
    targetId: target.id
    groupId: target.group
    dnsZoneIds: target.zones
    tags: tags
  }
}]
