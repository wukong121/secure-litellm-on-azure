@description('Azure region for the Private Endpoint.')
param location string

@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('Existing dedicated Private Endpoint subnet name.')
param privateEndpointSubnetName string

@description('Key Vault resource ID.')
param keyVaultResourceId string

@description('Stable Key Vault alias for naming.')
param keyVaultAlias string

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
  name: 'privatelink.vaultcore.azure.net'
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${keyVaultAlias}-vault'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-pe-${keyVaultAlias}-vault'
    privateLinkServiceConnections: [
      {
        name: '${keyVaultAlias}-vault-connection'
        properties: {
          groupIds: [
            'vault'
          ]
          privateLinkServiceId: keyVaultResourceId
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
        name: 'key-vault-private-dns-zone'
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
