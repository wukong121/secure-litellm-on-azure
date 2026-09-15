targetScope = 'resourceGroup'

@description('Existing AKS cluster name.')
param aksClusterName string

@description('Existing virtual network name.')
param virtualNetworkName string

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

var resolvedControlPlanePrincipalId = empty(controlPlanePrincipalId) ? cluster.identity.principalId : controlPlanePrincipalId

resource aksNetworkRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    virtualNetwork.id,
    cluster.id,
    networkContributorRoleId
  )
  scope: virtualNetwork
  properties: {
    principalId: resolvedControlPlanePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: networkContributorRoleId
    description: 'Allows only the AKS control plane to manage node and internal load balancer networking in the dedicated gateway virtual network.'
  }
}

output roleAssignmentId string = aksNetworkRole.id
