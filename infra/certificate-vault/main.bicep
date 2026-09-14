@minLength(3)
@maxLength(24)
param vaultName string
param location string
param ingressReaderPrincipalId string
param certificateImporterPrincipalId string
@allowed([
  'User'
  'Group'
])
param certificateImporterPrincipalType string
param virtualNetworkName string
param privateEndpointSubnetName string
param runnerVirtualNetworkId string
param logAnalyticsWorkspaceName string
param createPrivateDnsZone bool
param manageRunnerDnsLink bool
param manageTargetDnsLink bool = true
param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: vaultName
  location: location
  tags: union(tags, { purpose: 'ingress-certificates' })
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    accessPolicies: []
    enableRbacAuthorization: true
    enablePurgeProtection: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(toLower(vault.id), toLower(ingressReaderPrincipalId), 'certificate-reader')
  scope: vault
  properties: {
    principalId: ingressReaderPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    description: 'Ingress publisher reads API/admin certificate material; no application workload grant.'
  }
}

resource importer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(toLower(vault.id), toLower(certificateImporterPrincipalId), 'certificate-importer')
  scope: vault
  properties: {
    principalId: certificateImporterPrincipalId
    principalType: certificateImporterPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
    description: 'Approved human imports leaf certificate chains and private keys into the certificate-only Vault.'
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
    workspaceId: workspace.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [{ category: 'AuditEvent', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

var zoneName = 'privatelink.vaultcore.azure.net'

resource createdZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (createPrivateDnsZone) {
  name: zoneName
  location: 'global'
  tags: tags
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: zoneName
}

resource targetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (manageTargetDnsLink) {
  parent: zone
  name: '${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: virtualNetwork.id }
  }
  dependsOn: [createdZone]
}

resource runnerLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (manageRunnerDnsLink && toLower(runnerVirtualNetworkId) != toLower(virtualNetwork.id)) {
  parent: zone
  name: 'certificate-runner-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: runnerVirtualNetworkId }
  }
  dependsOn: [createdZone]
}

module endpoint '../modules/key-vault-private-endpoint/main.bicep' = {
  name: 'certificate-vault-private-endpoint'
  params: {
    location: location
    virtualNetworkName: virtualNetworkName
    privateEndpointSubnetName: privateEndpointSubnetName
    keyVaultResourceId: vault.id
    keyVaultAlias: vaultName
    tags: tags
  }
  dependsOn: [createdZone]
}

output certificateVault object = {
  id: vault.id
  name: vault.name
  uri: vault.properties.vaultUri
  privateEndpointName: endpoint.outputs.privateEndpoint.name
  privateDnsZoneId: zone.id
  apiTlsSecretId: '${vault.properties.vaultUri}secrets/api-tls'
  adminTlsSecretId: '${vault.properties.vaultUri}secrets/admin-tls'
  certificateMaterialsImported: false
}
