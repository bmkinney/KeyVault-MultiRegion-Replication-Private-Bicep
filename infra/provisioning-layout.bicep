targetScope = 'subscription'

@minLength(3)
@maxLength(8)
param workload string = 'mrkv'
param primaryLocation string = 'eastus'
param secondaryLocation string = 'centralus'
param tags object = { workload: workload, purpose: 'private-secret-replication-demo' }

var token = take(uniqueString(subscription().id, workload), 8)
resource primary 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: 'rg-${workload}-p-${token}'
  location: primaryLocation
  tags: tags
}
resource secondary 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: 'rg-${workload}-s-${token}'
  location: secondaryLocation
  tags: tags
}
