using './main.bicep'
param deployAuditStorage = false
param location = 'westus'
param logAnalyticsWorkspaceName = 'REPLACE_LOG_ANALYTICS_WORKSPACE'
param cmkVaultName = 'REPLACE_CMK_VAULT'
param cmkKeyName = 'REPLACE_CMK_KEY'
param writerPrincipalId = 'REPLACE_API_PROXY_PRINCIPAL_ID'
param readerPrincipalId = 'REPLACE_ADMIN_PROXY_PRINCIPAL_ID'
param retentionPrincipalId = 'REPLACE_RETENTION_WORKER_PRINCIPAL_ID'
