@description('Endpoint name.')
param name string
@description('Endpoint/VNet region.')
param location string
@description('Undelegated endpoint subnet ID.')
param subnetId string
@description('Target resource ID.')
param targetId string
@description('Private Link subresource.')
param groupId string
@description('Private DNS zone IDs for this endpoint.')
param dnsZoneIds array
@description('Resource tags.')
param tags object

resource endpoint 'Microsoft.Network/privateEndpoints@2025-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: name
        properties: {
          privateLinkServiceId: targetId
          groupIds: [groupId]
        }
      }
    ]
  }
}

resource dns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2025-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for (zoneId, index) in dnsZoneIds: {
      name: 'zone-${index}'
      properties: {
        privateDnsZoneId: zoneId
      }
    }]
  }
}
