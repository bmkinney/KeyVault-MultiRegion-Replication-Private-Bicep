@description('Regional naming prefix.')
param prefix string
@description('Regional location.')
param location string
@description('Resource tags.')
param tags object

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${prefix}-worker'
  location: location
  tags: tags
}

output principalId string = identity.properties.principalId
