targetScope = 'resourceGroup'

param virtualNetworkName string
param remoteVirtualNetworkId string
param connectionName string

resource network 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource peering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-07-01' = {
  parent: network
  name: connectionName
  properties: {
    remoteVirtualNetwork: { id: remoteVirtualNetworkId }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}
