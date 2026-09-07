@description('Azure region for the Private Endpoint.')
param location string

@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('Existing dedicated Private Endpoint subnet name.')
param privateEndpointSubnetName string

@description('Target Premium ACR resource ID.')
param registryResourceId string

@description('Target ACR name, used for deterministic resource naming only.')
param registryName string

@description('Resource tags.')
param tags object = {}

var privateDnsZoneName = 'privatelink.azurecr.io'

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
  name: 'pe-${registryName}-registry'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-pe-${registryName}-registry'
    privateLinkServiceConnections: [
      {
        name: '${registryName}-registry-connection'
        properties: {
          groupIds: [
            'registry'
          ]
          privateLinkServiceId: registryResourceId
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
        name: 'acr-private-dns-zone'
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
  privateDnsZoneId: privateDnsZone.id
}
