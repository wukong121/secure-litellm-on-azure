targetScope = 'resourceGroup'

@description('Azure region for Log Analytics query alert rules.')
param location string = resourceGroup().location

@description('Existing AKS cluster name.')
param aksClusterName string

@description('Existing Log Analytics workspace name used by Container Insights.')
param logAnalyticsWorkspaceName string

@description('Email address receiving the minimum Stage 1 alerts.')
param ownerEmail string

@description('Resource tags applied to alert resources.')
param tags object = {
  workload: 'litellm'
  purpose: 'stage1-minimum-alerting'
  owner: ownerEmail
  managedBy: 'bicep'
}

var actionGroupName = 'ag-litellm-stage1-owner'
var actionGroupShortName = 'litellm-s1'

resource aksCluster 'Microsoft.ContainerService/managedClusters@2025-01-01' existing = {
  name: aksClusterName
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'global'
  tags: tags
  properties: {
    enabled: true
    groupShortName: actionGroupShortName
    emailReceivers: [
      {
        name: 'litellm-owner-email'
        emailAddress: ownerEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

resource criticalPostgresLogAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-litellm-postgres-critical-log'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'LiteLLM PostgreSQL critical database log'
    description: 'Detects disk-full, PANIC, fatal shutdown, and recovery-loop evidence in the in-cluster PostgreSQL logs.'
    enabled: true
    severity: 0
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    autoMitigate: true
    scopes: [
      logAnalyticsWorkspace.id
    ]
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    criteria: {
      allOf: [
        {
          query: '''
            ContainerLogV2
            | where TimeGenerated > ago(10m)
            | where PodNamespace == "litellm" and PodName startswith "postgres-"
            | where LogMessage has_any (
                "No space left on device",
                "PANIC",
                "database system is in recovery mode",
                "database system is shut down",
                "could not write to file"
              )
            | summarize AggregatedValue = count()
            | where AggregatedValue > 0
          '''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            minFailingPeriodsToAlert: 1
            numberOfEvaluationPeriods: 1
          }
        }
      ]
    }
  }
}

resource litellmErrorBurstAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-litellm-error-burst'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'LiteLLM error burst'
    description: 'Detects repeated database/authentication 401 or 503 evidence and repeated unhandled errors in LiteLLM logs.'
    enabled: true
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    autoMitigate: true
    scopes: [
      logAnalyticsWorkspace.id
    ]
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    criteria: {
      allOf: [
        {
          query: '''
            ContainerLogV2
            | where TimeGenerated > ago(10m)
            | where PodNamespace == "litellm" and PodName startswith "litellm-mi-proxy-"
            | where LogMessage matches regex @"(?i)(database|prisma|authentication|authorization).*(401|503|error|failed|unavailable)"
                or LogMessage matches regex @"(?i)(401|503).*(database|prisma|authentication|authorization)"
                or LogMessage has_any ("Unhandled exception", "Traceback (most recent call last)")
            | summarize AggregatedValue = count()
            | where AggregatedValue > 4
          '''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            minFailingPeriodsToAlert: 1
            numberOfEvaluationPeriods: 1
          }
        }
      ]
    }
  }
}

resource workloadUnavailableAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-litellm-workload-unavailable'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'LiteLLM or PostgreSQL workload unavailable'
    description: 'Alerts when Container Insights has no recent Running inventory for the expected LiteLLM or PostgreSQL workload.'
    enabled: true
    severity: 0
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    autoMitigate: true
    scopes: [
      logAnalyticsWorkspace.id
    ]
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    criteria: {
      allOf: [
        {
          query: '''
            let RecentRunningWorkloads = KubePodInventory
              | where TimeGenerated > ago(10m)
              | where Namespace == "litellm" and PodStatus =~ "Running"
              | extend ExpectedWorkload = case(
                  Name startswith "litellm-mi-proxy-", "litellm-mi-proxy",
                  Name startswith "postgres-", "postgres",
                  "other"
                )
              | where ExpectedWorkload != "other"
              | summarize by ExpectedWorkload;
            datatable(ExpectedWorkload:string) ["litellm-mi-proxy", "postgres"]
            | join kind=leftanti RecentRunningWorkloads on ExpectedWorkload
            | summarize AggregatedValue = count()
            | where AggregatedValue > 0
          '''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            minFailingPeriodsToAlert: 1
            numberOfEvaluationPeriods: 1
          }
        }
      ]
    }
  }
}

resource postgresVolumeWarningAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-litellm-postgres-volume-70-percent'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'LiteLLM PostgreSQL volume above 70 percent'
    description: 'Early warning when the pg-data persistent volume usage exceeds 70 percent.'
    enabled: true
    severity: 2
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    autoMitigate: true
    scopes: [
      logAnalyticsWorkspace.id
    ]
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    criteria: {
      allOf: [
        {
          query: '''
            InsightsMetrics
            | where TimeGenerated > ago(15m)
            | where Namespace == "container.azm.ms/pv" and Name == "pvUsedBytes"
            | extend VolumeTags = parse_json(Tags)
            | where tostring(VolumeTags.pvcNamespace) == "litellm"
              and tostring(VolumeTags.pvcName) == "pg-data"
            | extend UsedPercent = 100.0 * todouble(Val) / todouble(VolumeTags.pvCapacityBytes)
            | summarize AggregatedValue = max(UsedPercent)
            | where isnotnull(AggregatedValue) and AggregatedValue > 70
          '''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            minFailingPeriodsToAlert: 1
            numberOfEvaluationPeriods: 1
          }
        }
      ]
    }
  }
}

resource postgresVolumeCriticalAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-litellm-postgres-volume-85-percent'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'LiteLLM PostgreSQL volume above 85 percent'
    description: 'Critical alert when the pg-data persistent volume usage exceeds 85 percent.'
    enabled: true
    severity: 0
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    autoMitigate: true
    scopes: [
      logAnalyticsWorkspace.id
    ]
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    criteria: {
      allOf: [
        {
          query: '''
            InsightsMetrics
            | where TimeGenerated > ago(10m)
            | where Namespace == "container.azm.ms/pv" and Name == "pvUsedBytes"
            | extend VolumeTags = parse_json(Tags)
            | where tostring(VolumeTags.pvcNamespace) == "litellm"
              and tostring(VolumeTags.pvcName) == "pg-data"
            | extend UsedPercent = 100.0 * todouble(Val) / todouble(VolumeTags.pvCapacityBytes)
            | summarize AggregatedValue = max(UsedPercent)
            | where isnotnull(AggregatedValue) and AggregatedValue > 85
          '''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            minFailingPeriodsToAlert: 1
            numberOfEvaluationPeriods: 1
          }
        }
      ]
    }
  }
}

resource aksFailedAdministrativeOperationAlert 'Microsoft.Insights/activityLogAlerts@2020-10-01' = {
  name: 'alert-litellm-aks-failed-administrative-operation'
  location: 'global'
  tags: tags
  properties: {
    description: 'Alerts when an Azure administrative operation on the LiteLLM AKS cluster fails.'
    enabled: true
    scopes: [
      aksCluster.id
    ]
    condition: {
      allOf: [
        {
          field: 'category'
          equals: 'Administrative'
        }
        {
          field: 'status'
          equals: 'Failed'
        }
      ]
    }
    actions: {
      actionGroups: [
        {
          actionGroupId: actionGroup.id
          webhookProperties: {}
        }
      ]
    }
  }
}

resource aksDeleteAlert 'Microsoft.Insights/activityLogAlerts@2020-10-01' = {
  name: 'alert-litellm-aks-delete'
  location: 'global'
  tags: tags
  properties: {
    description: 'Alerts when deletion of the LiteLLM AKS cluster is started.'
    enabled: true
    scopes: [
      aksCluster.id
    ]
    condition: {
      allOf: [
        {
          field: 'category'
          equals: 'Administrative'
        }
        {
          field: 'operationName'
          equals: 'Microsoft.ContainerService/managedClusters/delete'
        }
      ]
    }
    actions: {
      actionGroups: [
        {
          actionGroupId: actionGroup.id
          webhookProperties: {}
        }
      ]
    }
  }
}

output minimumAlerting object = {
  actionGroupName: actionGroup.name
  scheduledQueryAlerts: [
    criticalPostgresLogAlert.name
    litellmErrorBurstAlert.name
    workloadUnavailableAlert.name
    postgresVolumeWarningAlert.name
    postgresVolumeCriticalAlert.name
  ]
  activityLogAlerts: [
    aksFailedAdministrativeOperationAlert.name
    aksDeleteAlert.name
  ]
}
