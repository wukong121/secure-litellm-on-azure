targetScope = 'resourceGroup'

param location string
param logAnalyticsWorkspaceName string
@allowed(['create', 'existing'])
param workspaceMode string = 'create'
param tags object = {}

module logging '../modules/log-workspace/main.bicep' = {
  name: 'legacy-log-workspace'
  params: {
    location: location
    workspaceName: logAnalyticsWorkspaceName
    createWorkspace: workspaceMode == 'create'
    tags: tags
  }
}

output logging object = {
  workspaceId: logging.outputs.workspaceId
}