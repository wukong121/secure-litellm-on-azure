@description('Azure region for the Private Endpoint.')
param location string

@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('Existing dedicated Private Endpoint subnet name.')
param privateEndpointSubnetName string

@description('PostgreSQL Flexible Server resource ID.')
param serverResourceId string

@description('Stable PostgreSQL alias for naming.')
param serverAlias string

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
  name: 'privatelink.postgres.database.azure.com'
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${serverAlias}-postgresql'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-pe-${serverAlias}-postgresql'
    privateLinkServiceConnections: [
      {
        name: '${serverAlias}-postgresql-connection'
        properties: {
          groupIds: [
            'postgresqlServer'
          ]
          privateLinkServiceId: serverResourceId
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
        name: 'postgresql-private-dns-zone'
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
