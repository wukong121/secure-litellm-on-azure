@description('Existing LiteLLM security VNet name created by the backup foundation.')
param virtualNetworkName string

@description('Dedicated Azure Firewall subnet prefix. Azure requires the subnet name AzureFirewallSubnet.')
param firewallSubnetPrefix string

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource firewallSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: 'AzureFirewallSubnet'
  properties: {
    addressPrefix: firewallSubnetPrefix
    privateEndpointNetworkPolicies: 'Disabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
  }
}

output networkFoundation object = {
  virtualNetworkId: virtualNetwork.id
  virtualNetworkName: virtualNetwork.name
  firewallSubnetId: firewallSubnet.id
}
