@description('Globally unique Key Vault name.')
@minLength(3)
@maxLength(24)
param name string

@description('Azure region for the vault.')
param location string

@description('LiteLLM Workload Identity principal ID.')
param workloadPrincipalId string

@description('Existing Log Analytics workspace resource ID.')
param logAnalyticsWorkspaceId string

@description('Resource tags.')
param tags object = {}

var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    accessPolicies: []
    enablePurgeProtection: true
    enableRbacAuthorization: true
    enableSoftDelete: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
    publicNetworkAccess: 'Disabled'
    sku: {
      family: 'A'
      name: 'premium'
    }
    softDeleteRetentionInDays: 90
    tenantId: tenant().tenantId
  }
}

resource secretsUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, workloadPrincipalId, keyVaultSecretsUserRoleId)
  scope: vault
  properties: {
    description: 'Allow only the LiteLLM Workload Identity to read secret values at runtime.'
    principalId: workloadPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleId
  }
}

resource deleteLock 'Microsoft.Authorization/locks@2020-05-01' = {
  scope: vault
  name: 'protect-key-vault-from-deletion'
  properties: {
    level: 'CanNotDelete'
    notes: 'Removal requires an explicit, audited unlock operation.'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: vault
  name: 'send-key-vault-audit-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'AuditEvent'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output keyVault object = {
  id: vault.id
  name: vault.name
  uri: vault.properties.vaultUri
}
