targetScope = 'resourceGroup'

param location string
param aksClusterName string
@minLength(3)
@maxLength(24)
param cmkVaultName string
param cmkKeyName string
param virtualNetworkName string
param privateEndpointSubnetName string
param tags object = {}

resource cluster 'Microsoft.ContainerService/managedClusters@2025-01-01' existing = {
  name: aksClusterName
}

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: cmkVaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'premium' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices', ipRules: [], virtualNetworkRules: [] }
  }
}

resource key 'Microsoft.KeyVault/vaults/keys@2025-05-01' = {
  parent: vault
  name: cmkKeyName
  properties: {
    kty: 'RSA-HSM'
    keySize: 3072
    keyOps: ['wrapKey', 'unwrapKey']
    attributes: { enabled: true }
  }
}

module endpoint '../modules/key-vault-private-endpoint/main.bicep' = {
  name: 'audit-vault-private-endpoint'
  params: {
    location: location
    virtualNetworkName: virtualNetworkName
    privateEndpointSubnetName: privateEndpointSubnetName
    keyVaultResourceId: vault.id
    keyVaultAlias: 'audit'
    tags: tags
  }
}

var roles = [
  { key: 'writer', account: 'llm-api-proxy' }
  { key: 'reader', account: 'llm-admin-proxy' }
  { key: 'retention', account: 'l3-retention' }
  { key: 'recovery', account: 'l3-recovery' }
]

module identities '../modules/workload-identity/main.bicep' = [for role in roles: {
  name: 'audit-${role.key}-identity'
  params: {
    identityName: 'id-llmgw-audit-${role.key}-${uniqueString(resourceGroup().id)}'
    location: location
    oidcIssuerUrl: cluster.properties.oidcIssuerProfile.issuerURL
    kubernetesNamespace: 'litellm'
    serviceAccountName: role.account
    tags: tags
  }
}]

output auditFoundation object = {
  cmkVaultName: vault.name
  cmkKeyName: key.name
  writer: identities[0].outputs.workloadIdentity
  reader: identities[1].outputs.workloadIdentity
  retention: identities[2].outputs.workloadIdentity
  recovery: identities[3].outputs.workloadIdentity
}
