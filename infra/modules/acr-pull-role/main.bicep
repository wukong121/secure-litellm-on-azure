@description('Existing ACR name.')
param registryName string

@description('AKS kubelet managed identity object ID.')
param kubeletPrincipalId string

@description('Optional stable source resource ID for first-deployment What-if. Empty preserves existing principal-based assignment names.')
param principalSourceResourceId string = ''

var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, empty(principalSourceResourceId) ? kubeletPrincipalId : principalSourceResourceId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: kubeletPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
    description: 'Allows only the target AKS kubelet identity to pull approved images.'
  }
}

output roleAssignmentId string = acrPullRole.id
