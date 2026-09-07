@description('Azure region for network resources.')
param location string

@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('AKS system node subnet name.')
param systemSubnetName string

@description('AKS system node subnet prefix.')
param systemSubnetPrefix string

@description('AKS user node subnet name.')
param userSubnetName string

@description('AKS user node subnet prefix.')
param userSubnetPrefix string

@description('Private ingress subnet name.')
param ingressSubnetName string

@description('Private ingress subnet prefix.')
param ingressSubnetPrefix string

@description('Azure Firewall private IP used as the default route next hop.')
param firewallPrivateIpAddress string

@description('Resource tags.')
param tags object = {}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource nodeNetworkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: 'nsg-litellm-aks-nodes'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource ingressNetworkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: 'nsg-litellm-private-ingress'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource egressRouteTable 'Microsoft.Network/routeTables@2024-07-01' = {
  name: 'rt-litellm-aks-egress'
  location: location
  tags: tags
  properties: {
    disableBgpRoutePropagation: false
    routes: [
      {
        name: 'default-via-azure-firewall'
        properties: {
          addressPrefix: '0.0.0.0/0'
          nextHopIpAddress: firewallPrivateIpAddress
          nextHopType: 'VirtualAppliance'
        }
      }
    ]
  }
}

resource systemSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: systemSubnetName
  properties: {
    addressPrefix: systemSubnetPrefix
    networkSecurityGroup: {
      id: nodeNetworkSecurityGroup.id
    }
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
    routeTable: {
      id: egressRouteTable.id
    }
    serviceEndpoints: [
      {
        service: 'Microsoft.Storage'
      }
    ]
  }
}

resource userSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: userSubnetName
  properties: {
    addressPrefix: userSubnetPrefix
    networkSecurityGroup: {
      id: nodeNetworkSecurityGroup.id
    }
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
    routeTable: {
      id: egressRouteTable.id
    }
    serviceEndpoints: [
      {
        service: 'Microsoft.Storage'
      }
    ]
  }
}

resource ingressSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: ingressSubnetName
  properties: {
    addressPrefix: ingressSubnetPrefix
    networkSecurityGroup: {
      id: ingressNetworkSecurityGroup.id
    }
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Disabled'
    routeTable: {
      id: egressRouteTable.id
    }
  }
}

output aksNetwork object = {
  systemSubnetId: systemSubnet.id
  userSubnetId: userSubnet.id
  ingressSubnetId: ingressSubnet.id
  routeTableId: egressRouteTable.id
  nodeNetworkSecurityGroupId: nodeNetworkSecurityGroup.id
}
