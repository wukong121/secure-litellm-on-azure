targetScope = 'resourceGroup'

@description('Explicitly enable the new L3 account. No resources are created by default.')
param deployAuditStorage bool = false
param location string = resourceGroup().location
param storageAccountName string = 'stllaudit${uniqueString(resourceGroup().id)}'
param virtualNetworkName string = 'litellm-security-vnet'
param privateEndpointSubnetName string = 'snet-private-endpoints'
param logAnalyticsWorkspaceName string
@description('Dedicated, purge-protected CMK vault with Azure Storage service access configured.')
param cmkVaultName string
param cmkKeyName string
param writerPrincipalId string
param readerPrincipalId string
param retentionPrincipalId string
param tags object = {}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
}
resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
}
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}
resource cmkVault 'Microsoft.KeyVault/vaults@2025-05-01' existing = {
  name: cmkVaultName
}
resource cmkKey 'Microsoft.KeyVault/vaults/keys@2025-05-01' existing = {
  parent: cmkVault
  name: cmkKeyName
}
resource encryptionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (deployAuditStorage) {
  name: '${storageAccountName}-cmk'
  location: location
  tags: tags
}
var cryptoRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'e147488a-f6f5-4113-8e2d-b22465e65bf6')
resource cryptoAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAuditStorage) {
  scope: cmkKey
  name: guid(cmkKey.id, storageAccountName, cryptoRoleId)
  properties: {
    principalId: encryptionIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: cryptoRoleId
  }
}
resource account 'Microsoft.Storage/storageAccounts@2025-06-01' = if (deployAuditStorage) {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${encryptionIdentity!.id}': {} }
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    allowCrossTenantReplication: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Disabled'
    isHnsEnabled: false
    isLocalUserEnabled: false
    networkAcls: { bypass: 'None', defaultAction: 'Deny' }
    encryption: {
      keySource: 'Microsoft.Keyvault'
      requireInfrastructureEncryption: true
      identity: { userAssignedIdentity: encryptionIdentity!.id }
      keyvaultproperties: {
        keyname: cmkKeyName
        keyvaulturi: cmkVault.properties.vaultUri
      }
      services: { blob: { enabled: true, keyType: 'Account' } }
    }
  }
  dependsOn: [cryptoAccess]
}
resource service 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = if (deployAuditStorage) {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: false
    deleteRetentionPolicy: { enabled: false }
    containerDeleteRetentionPolicy: { enabled: false }
  }
}
var containerNames = ['l3-content', 'l3-index', 'l3-pending', 'l3-access']
resource auditContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = [for name in containerNames: if (deployAuditStorage) {
  parent: service
  name: name
  properties: {
    publicAccess: 'None'
    defaultEncryptionScope: '$account-encryption-key'
    denyEncryptionScopeOverride: true
  }
}]

var rolePolicies = [
  { alias: 'writer', actions: [], dataActions: ['Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'] }
  { alias: 'reader', actions: [], dataActions: ['Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'] }
  { alias: 'retention', actions: [], dataActions: ['Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read', 'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete'] }
  { alias: 'policy-reader', actions: ['Microsoft.Storage/storageAccounts/blobServices/read'], dataActions: [] }
]
resource roles 'Microsoft.Authorization/roleDefinitions@2022-04-01' = [for role in rolePolicies: if (deployAuditStorage) {
  name: guid(resourceGroup().id, storageAccountName, role.alias)
  properties: {
    roleName: '${storageAccountName}-${role.alias}'
    description: 'Dedicated L3 audit ${role.alias}; no shared-key access.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{ actions: role.actions, dataActions: role.dataActions, notActions: [], notDataActions: [] }]
  }
}]
var writerGrants = [for index in range(0, 3): { principalId: writerPrincipalId, containerIndex: index, roleIndex: 0 }]
var readerGrants = [for index in range(0, 2): { principalId: readerPrincipalId, containerIndex: index, roleIndex: 1 }]
var retentionGrants = [for index in range(0, 3): { principalId: retentionPrincipalId, containerIndex: index, roleIndex: 2 }]
var grants = concat(
  writerGrants,
  readerGrants,
  retentionGrants,
  [
    { principalId: readerPrincipalId, containerIndex: 3, roleIndex: 0 }
    { principalId: retentionPrincipalId, containerIndex: 3, roleIndex: 0 }
  ]
)
resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for grant in grants: if (deployAuditStorage) {
  scope: auditContainers[grant.containerIndex]
  name: guid(storageAccountName, containerNames[grant.containerIndex], grant.principalId, rolePolicies[grant.roleIndex].alias)
  properties: {
    principalId: grant.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles[grant.roleIndex].id
  }
}]
resource policyReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAuditStorage) {
  scope: account
  name: guid(storageAccountName, retentionPrincipalId, 'policy-reader')
  properties: {
    principalId: retentionPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles[3].id
  }
}
resource endpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = if (deployAuditStorage) {
  name: '${storageAccountName}-blob-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnet.id }
    privateLinkServiceConnections: [{
      name: '${storageAccountName}-blob'
      properties: { groupIds: ['blob'], privateLinkServiceId: account!.id }
    }]
  }
}
resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = if (deployAuditStorage) {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{ name: 'blob', properties: { privateDnsZoneId: blobZone.id } }]
  }
}
resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployAuditStorage) {
  scope: service
  name: 'l3-storage-access-audit'
  properties: {
    workspaceId: workspace.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [for category in ['StorageRead', 'StorageWrite', 'StorageDelete']: { category: category, enabled: true }]
    metrics: [{ category: 'Transaction', enabled: true }]
  }
}
resource deleteLock 'Microsoft.Authorization/locks@2020-05-01' = if (deployAuditStorage) {
  scope: account
  name: 'protect-l3-account'
  properties: { level: 'CanNotDelete', notes: 'Account deletion requires reviewed unlock; data retention is handled separately.' }
}
output storageUrl string = deployAuditStorage ? account!.properties.primaryEndpoints.blob : ''
output storageResourceId string = deployAuditStorage ? account!.id : ''
