targetScope = 'resourceGroup'

param location string
param namePrefix string
param addressPrefix string
param createBastion bool
param tags object

var runnerSubnetPrefix = cidrSubnet(addressPrefix, 26, 0)
var bastionSubnetPrefix = cidrSubnet(addressPrefix, 26, 1)

resource outboundIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: '${namePrefix}-egress-ip'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource nat 'Microsoft.Network/natGateways@2024-07-01' = {
  name: '${namePrefix}-nat'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [{ id: outboundIp.id }]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  name: '${namePrefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [addressPrefix] }
    subnets: concat([
      {
        name: 'snet-runner'
        properties: {
          addressPrefix: runnerSubnetPrefix
          defaultOutboundAccess: false
          natGateway: { id: nat.id }
        }
      }
    ], createBastion ? [
      {
        name: 'AzureBastionSubnet'
        properties: { addressPrefix: bastionSubnetPrefix }
      }
    ] : [])
  }
}

resource runnerSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: vnet
  name: 'snet-runner'
}

resource bastionSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: vnet
  name: 'AzureBastionSubnet'
}

resource bastionIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = if (createBastion) {
  name: '${namePrefix}-bastion-ip'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource bastion 'Microsoft.Network/bastionHosts@2024-07-01' = if (createBastion) {
  name: '${namePrefix}-bastion'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    scaleUnits: 2
    enableTunneling: true
    ipConfigurations: [{
      name: 'configuration'
      properties: {
        subnet: { id: bastionSubnet.id }
        publicIPAddress: { id: bastionIp!.id }
      }
    }]
  }
}

output vnetId string = vnet.id
output runnerSubnetId string = runnerSubnet.id
output bastionSubnetPrefix string = bastionSubnetPrefix
output bastionName string = createBastion ? bastion!.name : ''
output outboundPublicIp string = outboundIp.properties.ipAddress
