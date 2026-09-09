targetScope = 'resourceGroup'

param location string
param aksClusterName string
param logAnalyticsWorkspaceName string
param virtualNetworkName string
param privateEndpointSubnetName string
param tags object = {}

var suffix = uniqueString(resourceGroup().id)
resource cluster 'Microsoft.ContainerService/managedClusters@2025-01-01' existing = {
  name: aksClusterName
}
resource workspace 'Microsoft.OperationalInsights/workspaces@2025-02-01' existing = {
  name: logAnalyticsWorkspaceName
}
resource network 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: network
  name: privateEndpointSubnetName
}
module identity '../modules/workload-identity/main.bicep' = {
  name: 'collector-identity'
  params: {
    location: location
    identityName: 'id-llmgw-collector-${suffix}'
    oidcIssuerUrl: cluster.properties.oidcIssuerProfile.issuerURL
    kubernetesNamespace: 'litellm'
    serviceAccountName: 'otel-collector'
    tags: tags
  }
}
resource application 'Microsoft.Insights/components@2020-02-02' = {
  name: 'ai-llmgw-${suffix}'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
  }
}
var publisherRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
resource publish 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: application
  name: guid(application.id, 'collector', publisherRole)
  properties: {
    principalId: identity.outputs.workloadIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: publisherRole
  }
}
resource scope 'Microsoft.Insights/privateLinkScopes@2021-07-01-preview' = {
  name: 'ampls-llmgw-${suffix}'
  location: 'global'
  tags: tags
  properties: {
    accessModeSettings: {
      ingestionAccessMode: 'PrivateOnly'
      queryAccessMode: 'PrivateOnly'
      exclusions: []
    }
  }
}
resource appLink 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = {
  parent: scope
  name: 'application'
  properties: { linkedResourceId: application.id }
}
resource workspaceLink 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = {
  parent: scope
  name: 'workspace'
  properties: { linkedResourceId: workspace.id }
}
var zoneNames = ['privatelink.monitor.azure.com', 'privatelink.oms.opinsights.azure.com', 'privatelink.ods.opinsights.azure.com', 'privatelink.agentsvc.azure-automation.net']
resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zone in zoneNames: {
  name: zone
  location: 'global'
  tags: tags
}]
resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zone, index) in zoneNames: {
  parent: zones[index]
  name: 'llmgw-${suffix}'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: network.id } }
}]
resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
}
resource endpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'ampls-llmgw-${suffix}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnet.id }
    privateLinkServiceConnections: [{ name: 'monitor', properties: { privateLinkServiceId: scope.id, groupIds: ['azuremonitor'] } }]
  }
  dependsOn: [appLink, workspaceLink]
}
var monitorZones = [for (zone, index) in zoneNames: { name: 'monitor-${index}', properties: { privateDnsZoneId: zones[index].id } }]
resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: concat(monitorZones, [{ name: 'blob', properties: { privateDnsZoneId: blobZone.id } }])
  }
}
output observability object = {
  identity: identity.outputs.workloadIdentity
  applicationId: application.id
  connectionString: application.properties.ConnectionString
  privateLinkScopeId: scope.id
  workspaceId: workspace.id
  endpointSubnetId: subnet.id
}
