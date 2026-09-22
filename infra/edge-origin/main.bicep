targetScope = 'resourceGroup'

@description('Create a PLS only after the dedicated Standard internal load balancer frontend exists and is reviewed.')
param deployPrivateOrigin bool = false
param location string = resourceGroup().location
param privateLinkServiceName string = 'pls-llm-api'
param adminPrivateLinkServiceName string = 'pls-llm-admin'
param virtualNetworkName string = 'litellm-security-vnet'
param ingressSubnetName string = 'snet-private-ingress'

type internalLoadBalancerConfiguration = {
  resourceGroupName: string
  name: string
  frontendName: string
}
param apiLoadBalancer internalLoadBalancerConfiguration
param adminLoadBalancer internalLoadBalancerConfiguration
param tags object = {}

resource network 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: network
  name: ingressSubnetName
}
resource apiLoadBalancerResource 'Microsoft.Network/loadBalancers@2024-07-01' existing = {
  scope: resourceGroup(apiLoadBalancer.resourceGroupName)
  name: apiLoadBalancer.name
}
resource apiFrontend 'Microsoft.Network/loadBalancers/frontendIPConfigurations@2024-07-01' existing = {
  parent: apiLoadBalancerResource
  name: apiLoadBalancer.frontendName
}
resource adminLoadBalancerResource 'Microsoft.Network/loadBalancers@2024-07-01' existing = {
  scope: resourceGroup(adminLoadBalancer.resourceGroupName)
  name: adminLoadBalancer.name
}
resource adminFrontend 'Microsoft.Network/loadBalancers/frontendIPConfigurations@2024-07-01' existing = {
  parent: adminLoadBalancerResource
  name: adminLoadBalancer.frontendName
}
resource apiService 'Microsoft.Network/privateLinkServices@2024-07-01' = if (deployPrivateOrigin) {
  name: privateLinkServiceName
  location: location
  tags: tags
  properties: {
    enableProxyProtocol: false
    autoApproval: { subscriptions: [] }
    visibility: { subscriptions: ['*'] }
    loadBalancerFrontendIpConfigurations: [{ id: apiFrontend.id }]
    ipConfigurations: [{
      name: 'api-origin-nat'
      properties: {
        primary: true
        privateIPAddressVersion: 'IPv4'
        privateIPAllocationMethod: 'Dynamic'
        subnet: { id: subnet.id }
      }
    }]
  }
}

resource adminService 'Microsoft.Network/privateLinkServices@2024-07-01' = if (deployPrivateOrigin) {
  name: adminPrivateLinkServiceName
  location: location
  tags: tags
  properties: {
    enableProxyProtocol: false
    autoApproval: { subscriptions: [] }
    visibility: { subscriptions: ['*'] }
    loadBalancerFrontendIpConfigurations: [{ id: adminFrontend.id }]
    ipConfigurations: [{
      name: 'admin-origin-nat'
      properties: {
        primary: true
        privateIPAddressVersion: 'IPv4'
        privateIPAllocationMethod: 'Dynamic'
        subnet: { id: subnet.id }
      }
    }]
  }
}

output privateOrigin object = {
  privateLinkServiceId: deployPrivateOrigin ? apiService!.id : ''
  privateLinkLocation: location
}

output adminPrivateOrigin object = {
  privateLinkServiceId: deployPrivateOrigin ? adminService!.id : ''
  privateLinkLocation: location
}
