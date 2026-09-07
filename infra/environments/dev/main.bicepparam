using '../main.bicep'

param environmentName = 'dev'
param location = readEnvironmentVariable('AZURE_LOCATION', 'westus')
param logAnalyticsWorkspaceName = readEnvironmentVariable('LOG_ANALYTICS_WORKSPACE_NAME', 'example-workspace')
param deployContainerRegistry = false
param deployStage4 = false
param deployStage5 = false
param containerRegistryName = 'litellmdev${readEnvironmentVariable('LITELLM_ACR_SUFFIX', 'stage3check')}'
param containerRegistryPublicNetworkAccess = 'Disabled'
param tags = {
  costCenter: 'unassigned'
  dataClassification: 'confidential'
  owner: readEnvironmentVariable('OWNER_EMAIL', 'owner@example.com')
}
param stage4Network = {
  virtualNetworkName: 'litellm-security-vnet'
  privateEndpointSubnetName: 'snet-private-endpoints'
  firewallSubnetPrefix: '10.30.0.0/26'
  systemSubnetName: 'snet-aks-system'
  systemSubnetPrefix: '10.30.1.0/24'
  userSubnetName: 'snet-aks-user'
  userSubnetPrefix: '10.30.2.0/23'
  ingressSubnetName: 'snet-private-ingress'
  ingressSubnetPrefix: '10.30.4.0/24'
  podCidr: '10.244.0.0/16'
  serviceCidr: '10.31.0.0/16'
  dnsServiceIp: '10.31.0.10'
}
