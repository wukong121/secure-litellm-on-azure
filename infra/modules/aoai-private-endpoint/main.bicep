@description('Azure region for the Private Endpoint.')
param location string

@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('Existing dedicated Private Endpoint subnet name.')
param privateEndpointSubnetName string

@description('Target Azure AI/OpenAI account resource ID. Cross-subscription IDs must be provided through protected deployment variables, not committed parameter files.')
param accountResourceId string

@description('Non-sensitive stable alias used for Private Endpoint naming.')
param accountAlias string

@description('Resource tags.')
param tags object = {}

var privateDnsZoneName = 'privatelink.openai.azure.com'

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
}

resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: privateDnsZoneName
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${accountAlias}-account'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-pe-${accountAlias}-account'
    privateLinkServiceConnections: [
      {
        name: '${accountAlias}-account-connection'
        properties: {
          groupIds: [
            'account'
          ]
          privateLinkServiceId: accountResourceId
        }
      }
    ]
    subnet: {
      id: privateEndpointSubnet.id
    }
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'aoai-private-dns-zone'
        properties: {
          privateDnsZoneId: privateDnsZone.id
        }
      }
    ]
  }
}

output privateEndpoint object = {
  id: privateEndpoint.id
  name: privateEndpoint.name
}
