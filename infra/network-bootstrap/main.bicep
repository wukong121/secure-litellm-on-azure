targetScope = 'resourceGroup'

param location string = resourceGroup().location
param virtualNetworkName string
param virtualNetworkAddressPrefix string
param privateEndpointSubnetName string
param privateEndpointSubnetPrefix string
param tags object = {}

resource endpointSecurityGroup 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: 'nsg-litellm-private-endpoints'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  name: virtualNetworkName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [virtualNetworkAddressPrefix]
    }
    dhcpOptions: {
      dnsServers: []
    }
  }
}

resource endpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
  properties: {
    addressPrefix: privateEndpointSubnetPrefix
    networkSecurityGroup: {
      id: endpointSecurityGroup.id
    }
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
  }
}

output network object = {
  virtualNetworkId: virtualNetwork.id
  privateEndpointSubnetId: endpointSubnet.id
}