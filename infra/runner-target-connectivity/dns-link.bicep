param privateDnsZoneName string
param runnerVirtualNetworkId string
param linkName string
param tags object = {}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: privateDnsZoneName
}

resource runnerLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: linkName
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    resolutionPolicy: 'Default'
    virtualNetwork: { id: runnerVirtualNetworkId }
  }
}
