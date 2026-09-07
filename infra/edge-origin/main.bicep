targetScope = 'resourceGroup'

@description('Create a PLS only after the dedicated Standard internal load balancer frontend exists and is reviewed.')
param deployPrivateOrigin bool = false
param location string = resourceGroup().location
param privateLinkServiceName string = 'pls-llm-api'
param virtualNetworkName string = 'litellm-security-vnet'
param ingressSubnetName string = 'snet-private-ingress'

type internalLoadBalancerConfiguration = {
  resourceGroupName: string
  name: string
  frontendName: string
}
param apiLoadBalancer internalLoadBalancerConfiguration
param tags object = {}

resource network 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: network
  name: ingressSubnetName
}
resource loadBalancer 'Microsoft.Network/loadBalancers@2024-07-01' existing = {
  scope: resourceGroup(apiLoadBalancer.resourceGroupName)
  name: apiLoadBalancer.name
}
resource frontend 'Microsoft.Network/loadBalancers/frontendIPConfigurations@2024-07-01' existing = {
  parent: loadBalancer
  name: apiLoadBalancer.frontendName
}
resource service 'Microsoft.Network/privateLinkServices@2024-07-01' = if (deployPrivateOrigin) {
  name: privateLinkServiceName
  location: location
  tags: tags
  properties: {
    enableProxyProtocol: false
    autoApproval: { subscriptions: [] }
    visibility: { subscriptions: ['*'] }
    loadBalancerFrontendIpConfigurations: [{ id: frontend.id }]
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

output privateOrigin object = {
  privateLinkServiceId: deployPrivateOrigin ? service!.id : ''
  privateLinkLocation: location
}
