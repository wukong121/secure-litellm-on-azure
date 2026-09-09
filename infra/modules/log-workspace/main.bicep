param location string
param workspaceName string
param createWorkspace bool = true
@minValue(30)
@maxValue(730)
param retentionInDays int = 30
param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (createWorkspace) {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionInDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
      disableLocalAuth: true
    }
  }
}

output workspaceId string = resourceId('Microsoft.OperationalInsights/workspaces', workspaceName)