targetScope = 'resourceGroup'
param deployDetectionRules bool = false
param workspaceName string

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}
var rules = loadJsonContent('./rules.json')
resource detections 'Microsoft.SecurityInsights/alertRules@2024-03-01' = [for rule in rules: if (deployDetectionRules) {
  scope: workspace
  name: guid(workspace.id, rule.name)
  kind: 'Scheduled'
  properties: {
    displayName: rule.name
    description: 'Stage 8 metadata-only detection. Enable after customer log-quality and incident-response validation.'
    enabled: false
    severity: rule.severity
    query: rule.query
    queryFrequency: 'PT5M'
    queryPeriod: 'PT15M'
    triggerOperator: 'GreaterThan'
    triggerThreshold: 0
    suppressionEnabled: false
    suppressionDuration: 'PT1H'
  }
}]
