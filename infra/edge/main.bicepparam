using './main.bicep'

param deployEdge = false
param enableApiTraffic = false
param environmentName = 'test'
param baseDomain = 'example.com'
param privateOrigin = {
  privateLinkServiceId: 'REPLACE_API_PRIVATE_LINK_SERVICE_ID'
  privateLinkLocation: 'westus'
}
param logAnalyticsWorkspaceName = 'REPLACE_LOG_ANALYTICS_WORKSPACE'
param wafMode = 'Detection'
param rateLimitPerMinute = 600
