@description('Regional naming prefix.')
param prefix string
@description('VNet region.')
param location string
@description('Private endpoint subnet ID.')
param subnetId string
@description('DNS zone IDs indexed by service (vault, bus, blob, queue, table, monitor, oms, ods, agent). A missing or empty entry creates the endpoint without a DNS zone group.')
param zoneIds object
@description('Local vault ID.')
param localVaultId string
@description('Create the endpoint to the local vault. False when a reused vault already has one in this VNet.')
param createLocalVaultEndpoint bool = true
@description('Other-region vault ID.')
param remoteVaultId string
@description('Create the endpoint to the other-region vault. False when a reused vault already has one in this VNet.')
param createRemoteVaultEndpoint bool = true
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

var allTargets = [
  { suffix: 'vault-local', create: createLocalVaultEndpoint, id: localVaultId, group: 'vault', zones: [zoneIds.?vault ?? ''] }
  { suffix: 'vault-remote', create: createRemoteVaultEndpoint, id: remoteVaultId, group: 'vault', zones: [zoneIds.?vault ?? ''] }
  { suffix: 'bus', create: true, id: serviceBusId, group: 'namespace', zones: [zoneIds.?bus ?? ''] }
  { suffix: 'blob', create: true, id: storageId, group: 'blob', zones: [zoneIds.?blob ?? ''] }
  { suffix: 'queue', create: true, id: storageId, group: 'queue', zones: [zoneIds.?queue ?? ''] }
  { suffix: 'table', create: true, id: storageId, group: 'table', zones: [zoneIds.?table ?? ''] }
  { suffix: 'deadletter', create: true, id: deadletterStorageId, group: 'blob', zones: [zoneIds.?blob ?? ''] }
  {
    suffix: 'monitor'
    create: true
    id: monitorScopeId
    group: 'azuremonitor'
    zones: [zoneIds.?monitor ?? '', zoneIds.?oms ?? '', zoneIds.?ods ?? '', zoneIds.?agent ?? '', zoneIds.?blob ?? '']
  }
]
var targets = filter(allTargets, target => target.create)

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
