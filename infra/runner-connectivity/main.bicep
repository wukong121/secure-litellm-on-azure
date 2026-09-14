targetScope = 'resourceGroup'

param location string = resourceGroup().location
param tags object = {}
param runnerResourceGroupName string
param runnerVirtualNetworkName string
param backupVirtualNetworkName string
param connectionName string
param managePeering bool = true
param manageBlobDnsLink bool = true

resource runnerNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  scope: resourceGroup(runnerResourceGroupName)
  name: runnerVirtualNetworkName
}

resource backupNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: backupVirtualNetworkName
}

module runnerPeering 'peering.bicep' = if (managePeering) {
  name: connectionName
  scope: resourceGroup(runnerResourceGroupName)
  params: {
    virtualNetworkName: runnerVirtualNetworkName
    remoteVirtualNetworkId: backupNetwork.id
    connectionName: connectionName
  }
}

resource backupPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-07-01' = if (managePeering) {
  parent: backupNetwork
  name: connectionName
  properties: {
    remoteVirtualNetwork: { id: runnerNetwork.id }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
}

resource runnerDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (manageBlobDnsLink) {
  parent: blobZone
  name: connectionName
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: runnerNetwork.id }
  }
}

output runnerConnectivity object = {
  runnerVirtualNetworkId: runnerNetwork.id
  backupVirtualNetworkId: backupNetwork.id
  privateDnsZoneName: blobZone.name
  managePeering: managePeering
  manageBlobDnsLink: manageBlobDnsLink
  location: location
}
