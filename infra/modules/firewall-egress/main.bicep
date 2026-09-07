@description('Azure region for Azure Firewall resources.')
param location string

@description('Existing LiteLLM VNet name.')
param virtualNetworkName string

@description('Azure Firewall name.')
param firewallName string

@description('Azure Firewall Policy name.')
param firewallPolicyName string

@description('Public IP name used only by Azure Firewall egress.')
param publicIpName string

@description('AKS node subnet prefixes allowed to use approved HTTPS egress.')
param sourceAddressPrefixes array

@description('Approved HTTPS destination FQDNs. Keep this list explicit and reviewed.')
param approvedHttpsFqdns array

@description('Existing Log Analytics workspace resource ID for Firewall diagnostics.')
param logAnalyticsWorkspaceId string

@description('Resource tags.')
param tags object = {}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource firewallSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: virtualNetwork
  name: 'AzureFirewallSubnet'
}

resource firewallPublicIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: publicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource firewallPolicy 'Microsoft.Network/firewallPolicies@2024-07-01' = {
  name: firewallPolicyName
  location: location
  tags: tags
  properties: {
    dnsSettings: {
      enableProxy: true
    }
    intrusionDetection: {
      mode: 'Alert'
    }
    sku: {
      tier: 'Premium'
    }
    threatIntelMode: 'Alert'
  }
}

resource firewall 'Microsoft.Network/azureFirewalls@2024-07-01' = {
  name: firewallName
  location: location
  tags: tags
  properties: {
    firewallPolicy: {
      id: firewallPolicy.id
    }
    ipConfigurations: [
      {
        name: 'firewall-ip-configuration'
        properties: {
          publicIPAddress: {
            id: firewallPublicIp.id
          }
          subnet: {
            id: firewallSubnet.id
          }
        }
      }
    ]
    sku: {
      name: 'AZFW_VNet'
      tier: 'Premium'
    }
    threatIntelMode: 'Alert'
  }
}

resource firewallRuleCollectionGroup 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2024-07-01' = {
  parent: firewallPolicy
  name: 'litellm-egress-rules'
  properties: {
    priority: 200
    ruleCollections: [
      {
        name: 'allow-approved-https'
        priority: 200
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        action: {
          type: 'Allow'
        }
        rules: [
          {
            name: 'approved-https-fqdns'
            ruleType: 'ApplicationRule'
            sourceAddresses: sourceAddressPrefixes
            protocols: [
              {
                protocolType: 'Https'
                port: 443
              }
            ]
            targetFqdns: approvedHttpsFqdns
            terminateTLS: false
          }
        ]
      }
      {
        name: 'allow-platform-network'
        priority: 210
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        action: {
          type: 'Allow'
        }
        rules: [
          {
            name: 'azure-dns'
            ruleType: 'NetworkRule'
            sourceAddresses: sourceAddressPrefixes
            destinationAddresses: [
              '168.63.129.16'
            ]
            destinationPorts: [
              '53'
            ]
            ipProtocols: [
              'TCP'
              'UDP'
            ]
          }
          {
            name: 'network-time'
            ruleType: 'NetworkRule'
            sourceAddresses: sourceAddressPrefixes
            destinationAddresses: [
              '*'
            ]
            destinationPorts: [
              '123'
            ]
            ipProtocols: [
              'UDP'
            ]
          }
        ]
      }
    ]
  }
}

resource firewallDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: firewall
  name: 'send-firewall-logs-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output firewall object = {
  id: firewall.id
  name: firewall.name
  policyId: firewallPolicy.id
  privateIpAddress: firewall.properties.ipConfigurations[0].properties.privateIPAddress
}
