using './main.bicep'

param deployEdge = false
param enableApiTraffic = false
param environmentName = 'test'
param baseDomain = 'example.com'
param privateOrigin = {
  privateLinkServiceId: 'REPLACE_API_PRIVATE_LINK_SERVICE_ID'
  privateLinkLocation: 'westus'
}
param adminPrivateOrigin = {
  privateLinkServiceId: 'REPLACE_ADMIN_PRIVATE_LINK_SERVICE_ID'
  privateLinkLocation: 'westus'
}
param adminMtls = {
  keyVaultResourceGroupName: 'REPLACE_EDGE_TRUST_VAULT_RESOURCE_GROUP'
  keyVaultName: 'REPLACE_EDGE_TRUST_VAULT_NAME'
  allowedCertificateFqdns: [
    'REPLACE_CLIENT_CERTIFICATE_FQDN'
  ]
  trustedClientCaSecrets: [
    {
      secretName: 'REPLACE_CLIENT_CA_CHAIN_SECRET_NAME'
      secretVersion: 'REPLACE_CLIENT_CA_CHAIN_SECRET_VERSION'
    }
  ]
}
param logAnalyticsWorkspaceName = 'REPLACE_LOG_ANALYTICS_WORKSPACE'
param wafMode = 'Detection'
param rateLimitPerMinute = 600
param adminRateLimitPerMinute = 120
