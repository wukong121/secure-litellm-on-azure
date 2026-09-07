@description('Azure region for the Private Endpoint.')
param location string

@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('Existing dedicated Private Endpoint subnet name.')
param privateEndpointSubnetName string

@description('Azure Managed Redis resource ID.')
param redisResourceId string

@description('Stable Redis alias for naming.')
param redisAlias string

@description('Resource tags.')
param tags object = {}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'privatelink.redis.azure.net'
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${redisAlias}-redis'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-pe-${redisAlias}-redis'
    privateLinkServiceConnections: [
      {
        name: '${redisAlias}-redis-connection'
        properties: {
          groupIds: [
            'redisEnterprise'
          ]
          privateLinkServiceId: redisResourceId
        }
      }
    ]
    subnet: {
      id: subnet.id
    }
  }
}

resource dnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'managed-redis-private-dns-zone'
        properties: {
          privateDnsZoneId: zone.id
        }
      }
    ]
  }
}

output privateEndpoint object = {
  id: privateEndpoint.id
  name: privateEndpoint.name
}
