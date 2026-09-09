targetScope = 'resourceGroup'

param location string
param environmentName string
param aksClusterName string
param virtualNetworkName string
param privateEndpointSubnetName string
param logAnalyticsWorkspaceName string
param bootstrapPrincipalId string
param entraBootstrapPrincipalId string = ''
param tags object = {}

resource cluster 'Microsoft.ContainerService/managedClusters@2025-01-01' existing = {
  name: aksClusterName
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

var planes = ['api', 'admin']

module identities '../modules/workload-identity/main.bicep' = [for plane in planes: {
  name: 'proxy-${plane}-identity'
  params: {
    identityName: 'id-llmgw-${plane}-${environmentName}'
    location: location
    oidcIssuerUrl: cluster.properties.oidcIssuerProfile.issuerURL
    kubernetesNamespace: 'litellm'
    serviceAccountName: 'llm-${plane}-proxy'
    tags: tags
  }
}]

module vaults '../modules/key-vault/main.bicep' = [for (plane, index) in planes: {
  name: 'proxy-${plane}-vault'
  params: {
    name: 'kv-p-${plane}-${uniqueString(resourceGroup().id, environmentName)}'
    location: location
    workloadPrincipalId: identities[index].outputs.workloadIdentity.principalId
    bootstrapPrincipalId: bootstrapPrincipalId
    logAnalyticsWorkspaceId: workspace.id
    tags: tags
  }
}]

module endpoints '../modules/key-vault-private-endpoint/main.bicep' = [for (plane, index) in planes: {
  name: 'proxy-${plane}-vault-endpoint'
  params: {
    location: location
    virtualNetworkName: virtualNetworkName
    privateEndpointSubnetName: privateEndpointSubnetName
    keyVaultResourceId: vaults[index].outputs.keyVault.id
    keyVaultAlias: 'proxy-${plane}-${environmentName}'
    tags: tags
  }
}]

resource adminVault 'Microsoft.KeyVault/vaults@2025-05-01' existing = {
  name: 'kv-p-admin-${uniqueString(resourceGroup().id, environmentName)}'
}

resource entraCredentialWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(entraBootstrapPrincipalId) && entraBootstrapPrincipalId != bootstrapPrincipalId) {
  name: guid(adminVault.id, entraBootstrapPrincipalId, 'admin-oidc-bootstrap')
  scope: adminVault
  properties: {
    principalId: entraBootstrapPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  }
  dependsOn: [vaults]
}

output proxyFoundation object = {
  api: {
    identity: identities[0].outputs.workloadIdentity
    vault: vaults[0].outputs.keyVault
  }
  admin: {
    identity: identities[1].outputs.workloadIdentity
    vault: vaults[1].outputs.keyVault
  }
}
