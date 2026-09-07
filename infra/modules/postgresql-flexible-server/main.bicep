@description('Globally unique PostgreSQL Flexible Server name.')
@minLength(3)
@maxLength(63)
param name string

@description('Azure region for PostgreSQL.')
param location string

@description('Database created for LiteLLM.')
param databaseName string = 'litellm'

@description('Microsoft Entra administrator object ID. Supply only through protected deployment variables.')
param entraAdministratorObjectId string = ''

@description('Microsoft Entra administrator display name.')
param entraAdministratorPrincipalName string = ''

@description('Microsoft Entra administrator principal type.')
@allowed([
  'Group'
  'ServicePrincipal'
  'User'
])
param entraAdministratorPrincipalType string = 'Group'

@description('Existing Log Analytics workspace resource ID.')
param logAnalyticsWorkspaceId string

@description('PostgreSQL compute SKU.')
param skuName string = 'Standard_D2s_v3'

@description('High availability mode. West US must not assume zone redundancy.')
@allowed([
  'Disabled'
  'SameZone'
  'ZoneRedundant'
])
param highAvailabilityMode string = 'Disabled'

@description('Resource tags.')
param tags object = {}

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: 'GeneralPurpose'
  }
  properties: {
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
    backup: {
      backupRetentionDays: 14
      geoRedundantBackup: 'Disabled'
    }
    createMode: 'Create'
    highAvailability: {
      mode: highAvailabilityMode
    }
    network: {
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: 128
      type: 'Premium_LRS'
    }
    version: '16'
  }
}

resource administrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2025-08-01' = if (!empty(entraAdministratorObjectId)) {
  parent: server
  name: entraAdministratorObjectId
  properties: {
    principalName: entraAdministratorPrincipalName
    principalType: entraAdministratorPrincipalType
    tenantId: tenant().tenantId
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: server
  name: 'send-postgresql-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output postgresql object = {
  id: server.id
  name: server.name
  databaseName: database.name
  fqdn: server.properties.fullyQualifiedDomainName
  entraAdministratorConfigured: !empty(entraAdministratorObjectId)
}
