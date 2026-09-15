targetScope = 'resourceGroup'

@description('Existing AKS cluster name.')
param aksClusterName string

@description('Existing virtual network name.')
param virtualNetworkName string

@description('Existing private ingress subnet name.')
param ingressSubnetName string

@description('AKS control-plane managed identity object ID. Leave empty only when deploying this component after the cluster exists.')
param controlPlanePrincipalId string = ''

var networkContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4d97b98b-1d4f-4787-a291-c67834d212e7'
)

resource cluster 'Microsoft.ContainerService/managedClusters@2025-01-01' existing = {
  name: aksClusterName
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource ingressSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: virtualNetwork
  name: ingressSubnetName
}

var resolvedControlPlanePrincipalId = empty(controlPlanePrincipalId) ? cluster.identity.principalId : controlPlanePrincipalId

resource aksIngressSubnetRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    ingressSubnet.id,
    cluster.id,
    networkContributorRoleId
  )
  scope: ingressSubnet
  properties: {
    principalId: resolvedControlPlanePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: networkContributorRoleId
    description: 'Allows only the AKS control plane to provision internal load balancers in the private ingress subnet.'
  }
}

output roleAssignmentId string = aksIngressSubnetRole.id
