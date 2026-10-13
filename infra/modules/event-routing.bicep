@description('Existing Event Grid system topic.')
param topicName string
@description('Local queue destination ID.')
param queueId string
@description('Dead-letter storage ID.')
param deadletterStorageId string
@description('Allow events after worker verification; inactive routes have a nonmatching subject filter.')
param enabled bool
@description('Dedicated subscription name; do not overwrite an unrelated existing subscription.')
param subscriptionName string = 'secret-events'

resource topic 'Microsoft.EventGrid/systemTopics@2025-02-15' existing = {
  name: topicName
}

resource subscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2025-02-15' = {
  parent: topic
  name: subscriptionName
  properties: {
    labels: ['mrkv-replication']
    eventDeliverySchema: 'EventGridSchema'
    filter: {
      subjectBeginsWith: enabled ? '' : '__mrkv_inactive__/'
      includedEventTypes: [
        'Microsoft.KeyVault.SecretNewVersionCreated'
        'Microsoft.KeyVault.SecretNearExpiry'
        'Microsoft.KeyVault.SecretExpired'
      ]
    }
    deliveryWithResourceIdentity: {
      identity: {
        type: 'SystemAssigned'
      }
      destination: {
        endpointType: 'ServiceBusQueue'
        properties: {
          resourceId: queueId
        }
      }
    }
    deadLetterWithResourceIdentity: {
      identity: {
        type: 'SystemAssigned'
      }
      deadLetterDestination: {
        endpointType: 'StorageBlob'
        properties: {
          resourceId: deadletterStorageId
          blobContainerName: 'eventgrid-deadletter'
        }
      }
    }
    retryPolicy: {
      maxDeliveryAttempts: 30
      eventTimeToLiveInMinutes: 1440
    }
  }
}
