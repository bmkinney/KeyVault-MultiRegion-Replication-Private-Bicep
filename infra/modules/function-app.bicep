@description('Regional naming prefix.')
param prefix string
@description('App/plan/VNet location.')
param location string
@description('Foundation runtime configuration.')
param foundation object
@description('Expected source vault ARM ID.')
param sourceVaultId string
@description('Source vault URI.')
param sourceVaultUri string
@description('Destination vault URI.')
param destinationVaultUri string
@description('Destination vault ARM ID for collision lineage validation.')
param destinationVaultId string
@description('Explicit name allowlist.')
param secretNames array = []
@description('Selected name prefix.')
#disable-next-line secure-secrets-in-params // Name filter, never a secret value.
param secretPrefix string = ''
@description('Whole-vault replication opt-in.')
param replicateAllSecrets bool = false
@description('Explicit unmanaged destination overwrite approval.')
param allowUnmanagedOverwrite bool = false
@description('Enable replication and reconciliation. Never enable both regional workers.')
param enabled bool
@description('Resource tags.')
param tags object

resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(foundation.identityId, '/'))
  scope: resourceGroup(split(foundation.identityId, '/')[2], split(foundation.identityId, '/')[4])
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: last(split(foundation.insightsId, '/'))
  scope: resourceGroup(split(foundation.insightsId, '/')[4])
}

resource plan 'Microsoft.Web/serverfarms@2025-03-01' = {
  name: 'plan-${prefix}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource app 'Microsoft.Web/sites@2025-03-01' = {
  name: 'func-${prefix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${foundation.identityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Disabled'
    virtualNetworkSubnetId: foundation.integrationSubnetId
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: foundation.storageName }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: workerIdentity.properties.clientId }
        { name: 'ServiceBusConnection__fullyQualifiedNamespace', value: foundation.busNamespace }
        { name: 'ServiceBusConnection__credential', value: 'managedidentity' }
        { name: 'ServiceBusConnection__clientId', value: workerIdentity.properties.clientId }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'ClientId=${workerIdentity.properties.clientId};Authorization=AAD' }
        { name: 'WORKER_CLIENT_ID', value: workerIdentity.properties.clientId }
        { name: 'SOURCE_VAULT_ID', value: sourceVaultId }
        { name: 'SOURCE_VAULT_URI', value: sourceVaultUri }
        { name: 'DESTINATION_VAULT_URI', value: destinationVaultUri }
        { name: 'DESTINATION_VAULT_ID', value: destinationVaultId }
        { name: 'SECRET_NAMES', value: string(secretNames) }
        { name: 'SECRET_PREFIX', value: secretPrefix }
        { name: 'REPLICATE_ALL_SECRETS', value: string(replicateAllSecrets) }
        { name: 'ALLOW_UNMANAGED_OVERWRITE', value: string(allowUnmanagedOverwrite) }
        { name: 'COORDINATION_BLOB_URI', value: foundation.storageBlobUri }
        { name: 'REPLICATION_ENABLED', value: string(enabled) }
        { name: 'AzureWebJobs.Replicate.Disabled', value: string(!enabled) }
        { name: 'AzureWebJobs.Reconcile.Disabled', value: string(!enabled) }
        { name: 'RECONCILIATION_SCHEDULE', value: '0 */5 * * * *' }
      ]
    }
    functionAppConfig: {
      runtime: {
        name: 'python'
        version: '3.12'
      }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${foundation.storageBlobUri}deployment'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: foundation.identityId
          }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: 2048
        maximumInstanceCount: 40
        alwaysReady: [
          { name: 'function:Replicate', instanceCount: 1 }
          { name: 'function:Reconcile', instanceCount: 1 }
        ]
      }
    }
  }
}

resource scmAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: app
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: app
  name: 'ftp'
  properties: {
    allow: false
  }
}

module endpoint './private-endpoint.bicep' = {
  name: 'worker-endpoint'
  params: {
    name: 'pe-${prefix}-app'
    location: location
    subnetId: foundation.endpointSubnetId
    targetId: app.id
    groupId: 'sites'
    dnsZoneIds: [foundation.appDnsZoneId]
    tags: tags
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'workspace'
  scope: app
  properties: {
    workspaceId: foundation.workspaceId
    logs: [{ category: 'FunctionAppLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

resource workerAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-${prefix}-worker'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'Replication error, expiry, drift, or lag > 60 seconds'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [foundation.workspaceId]
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where Message startswith "MRKV " | extend report = parse_json(substring(Message, 5)) | where report.outcome in ("failure", "source_missing", "expired", "near_expiry", "destination_only") or (report.outcome == "source_inactive" and report.destinationExists == false) or todouble(report.lagSeconds) > 60'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [foundation.actionGroupId]
    }
  }
}

resource heartbeatAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-${prefix}-heartbeat'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'Active reconciliation heartbeat missing'
    severity: 1
    enabled: enabled
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [foundation.workspaceId]
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where AppRoleName == "${app.name}" | where Message startswith "MRKV " | extend report = parse_json(substring(Message, 5)) | where report.outcome == "reconciliation_complete" and toint(report.failures) == 0'
          timeAggregation: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [foundation.actionGroupId]
    }
  }
}

output name string = app.name
