@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('Resource tags.')
param tags object = {}

@description('Configure Stage 5 private DNS zones and VNet links.')
param configureStage5Zones bool = false

@description('Create the Key Vault private DNS zone. Keep false when a centrally managed zone already exists.')
param createKeyVaultZone bool = false

@description('Create the PostgreSQL private DNS zone. Keep false when a centrally managed zone already exists.')
param createPostgresqlZone bool = false

@description('Create the Azure Managed Redis private DNS zone when it does not already exist.')
param createManagedRedisZone bool = true

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource acrZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurecr.io'
  location: 'global'
  tags: tags
}

resource acrLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: acrZone
  name: '${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource azureOpenAIZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.openai.azure.com'
  location: 'global'
  tags: tags
}

resource azureOpenAILink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: azureOpenAIZone
  name: '${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

var keyVaultZoneName = 'privatelink.vaultcore.azure.net'
var postgresqlZoneName = 'privatelink.postgres.database.azure.com'
var managedRedisZoneName = 'privatelink.redis.azure.net'

resource keyVaultZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (configureStage5Zones && createKeyVaultZone) {
  name: keyVaultZoneName
  location: 'global'
  tags: tags
}

resource keyVaultLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (configureStage5Zones) {
  #disable-next-line use-parent-property
  name: '${keyVaultZoneName}/${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
  dependsOn: [
    keyVaultZone
  ]
}

resource postgresqlZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (configureStage5Zones && createPostgresqlZone) {
  name: postgresqlZoneName
  location: 'global'
  tags: tags
}

resource postgresqlLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (configureStage5Zones) {
  #disable-next-line use-parent-property
  name: '${postgresqlZoneName}/${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
  dependsOn: [
    postgresqlZone
  ]
}

resource managedRedisZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (configureStage5Zones && createManagedRedisZone) {
  name: managedRedisZoneName
  location: 'global'
  tags: tags
}

resource managedRedisLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (configureStage5Zones) {
  #disable-next-line use-parent-property
  name: '${managedRedisZoneName}/${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
  dependsOn: [
    managedRedisZone
  ]
}

output privateDns object = {
  acrZoneId: acrZone.id
  azureOpenAIZoneId: azureOpenAIZone.id
  keyVaultZoneId: resourceId('Microsoft.Network/privateDnsZones', keyVaultZoneName)
  postgresqlZoneId: resourceId('Microsoft.Network/privateDnsZones', postgresqlZoneName)
  managedRedisZoneId: resourceId('Microsoft.Network/privateDnsZones', managedRedisZoneName)
}
