targetScope = 'resourceGroup'

@description('Existing VNet in this resource group.')
param vnetName string
@description('Name of the Function integration subnet to add.')
param subnetName string
@description('Address prefix of the new subnet: /27 or larger, inside the VNet, and unused by another subnet.')
param addressPrefix string

resource vnet 'Microsoft.Network/virtualNetworks@2025-05-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2025-05-01' = {
  parent: vnet
  name: subnetName
  properties: {
    addressPrefix: addressPrefix
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

output id string = subnet.id
