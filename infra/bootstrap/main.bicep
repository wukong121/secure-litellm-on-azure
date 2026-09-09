targetScope = 'subscription'

param location string
param resourceGroupName string
param logAnalyticsWorkspaceName string
@allowed(['create', 'existing'])
param workspaceMode string = 'create'
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30
param tags object = {}

resource targetGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module logging '../modules/log-workspace/main.bicep' = {
  name: 'gateway-log-workspace'
  scope: targetGroup
  params: {
    location: location
    workspaceName: logAnalyticsWorkspaceName
    createWorkspace: workspaceMode == 'create'
    retentionInDays: logRetentionDays
    tags: tags
  }
}

output bootstrap object = {
  resourceGroupName: targetGroup.name
  logAnalyticsWorkspaceId: logging.outputs.workspaceId
  logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
}