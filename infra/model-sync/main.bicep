targetScope = 'resourceGroup'

param location string
param virtualNetworkName string
param privateEndpointSubnetName string
param accountResourceId string
param accountAlias string
param accountName string
param accountSubscriptionId string
param accountResourceGroupName string
param principalId string
param principalSourceResourceId string = ''
param createEndpoint bool
param createRole bool
param createDnsBinding bool
param endpointName string
param createDnsLink bool

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'privatelink.openai.azure.com'
}
resource vnet 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}
resource existingEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' existing = {
  name: endpointName
}
resource binding 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = if (createDnsBinding) {
  parent: existingEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'aoai-private-dns-zone'
        properties: {
          privateDnsZoneId: zone.id
        }
      }
    ]
  }
}
resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (createDnsLink) {
  parent: zone
  name: 'model-sync-${virtualNetworkName}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

module endpoint '../modules/aoai-private-endpoint/main.bicep' = if (createEndpoint) {
  name: 'model-sync-pe-${accountAlias}'
  params: {
    location: location
    virtualNetworkName: virtualNetworkName
    privateEndpointSubnetName: privateEndpointSubnetName
    accountResourceId: accountResourceId
    accountAlias: accountAlias
  }
}

module role '../modules/model-access-role/main.bicep' = if (createRole) {
  name: 'model-sync-role-${accountAlias}'
  scope: resourceGroup(accountSubscriptionId, accountResourceGroupName)
  params: {
    accountName: accountName
    principalId: principalId
    principalSourceResourceId: principalSourceResourceId
  }
}
