targetScope = 'resourceGroup'

@description('Create only new Stage 9 edge resources. Does not create DNS records or deploy an ingress controller.')
param deployEdge bool = false

@description('Separate traffic gate. Keep false until private origin, WAF association and release evidence are reviewed.')
param enableApiTraffic bool = false

@allowed(['dev', 'test', 'prod'])
param environmentName string = 'test'

@description('Environment domain supplied outside Git. Only llm-api is associated with Front Door.')
param baseDomain string = 'example.com'

type privateOriginConfiguration = {
  privateLinkServiceId: string
  privateLinkLocation: string
}

@description('Existing, reviewed API-only Private Link Service. Never use the admin ingress or the legacy public gateway.')
param privateOrigin privateOriginConfiguration

@description('Existing workspace for edge access, health and WAF diagnostics.')
param logAnalyticsWorkspaceName string

@allowed(['Detection', 'Prevention'])
param wafMode string = 'Detection'

@minValue(1)
@maxValue(100000)
@description('Per-client-IP rate threshold per minute; validate corporate NAT traffic before Prevention.')
param rateLimitPerMinute int = 600

param tags object = {}

var suffix = uniqueString(resourceGroup().id, environmentName)
var apiHost = 'llm-api.${baseDomain}'
var trafficState = enableApiTraffic ? 'Enabled' : 'Disabled'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource profile 'Microsoft.Cdn/profiles@2025-04-15' = if (deployEdge) {
  name: 'afd-llm-${environmentName}-${suffix}'
  location: 'global'
  tags: tags
  sku: { name: 'Premium_AzureFrontDoor' }
  properties: { originResponseTimeoutSeconds: 240 }
}

resource endpoint 'Microsoft.Cdn/profiles/afdEndpoints@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'llm-api-${environmentName}-${suffix}'
  location: 'global'
  properties: { enabledState: trafficState }
}

resource domain 'Microsoft.Cdn/profiles/customDomains@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'llm-api'
  properties: {
    hostName: apiHost
    tlsSettings: {
      certificateType: 'ManagedCertificate'
      minimumTlsVersion: 'TLS12'
    }
  }
}

resource originGroup 'Microsoft.Cdn/profiles/originGroups@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'private-api'
  properties: {
    sessionAffinityState: 'Disabled'
    healthProbeSettings: {
      probePath: '/readyz'
      probeProtocol: 'Https'
      probeRequestType: 'GET'
      probeIntervalInSeconds: 30
    }
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
      additionalLatencyInMilliseconds: 0
    }
  }
}

resource origin 'Microsoft.Cdn/profiles/originGroups/origins@2025-04-15' = if (deployEdge) {
  parent: originGroup
  name: 'private-api'
  properties: {
    hostName: apiHost
    originHostHeader: apiHost
    httpsPort: 443
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
    priority: 1
    weight: 1000
    sharedPrivateLinkResource: {
      privateLink: { id: privateOrigin.privateLinkServiceId }
      privateLinkLocation: privateOrigin.privateLinkLocation
      requestMessage: 'Stage 9 API origin only; verify target profile and manually approve after review.'
    }
  }
}

resource waf 'Microsoft.Network/frontDoorWebApplicationFirewallPolicies@2024-02-01' = if (deployEdge) {
  name: 'wafllm${environmentName}${suffix}'
  location: 'global'
  tags: tags
  sku: { name: 'Premium_AzureFrontDoor' }
  properties: {
    policySettings: {
      enabledState: 'Enabled'
      mode: wafMode
      requestBodyCheck: 'Enabled'
      logScrubbing: {
        state: 'Enabled'
        scrubbingRules: [for variable in ['RequestHeaderNames', 'RequestCookieNames', 'QueryStringArgNames', 'RequestBodyPostArgNames', 'RequestBodyJsonArgNames']: {
          matchVariable: variable
          selectorMatchOperator: 'EqualsAny'
          state: 'Enabled'
        }]
      }
    }
    managedRules: {
      managedRuleSets: [
        { ruleSetType: 'Microsoft_DefaultRuleSet', ruleSetVersion: '2.1' }
        { ruleSetType: 'Microsoft_BotManagerRuleSet', ruleSetVersion: '1.1' }
      ]
    }
    customRules: {
      rules: [
        {
          name: 'BlockNonPost'
          priority: 10
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [{ matchVariable: 'RequestMethod', operator: 'Equal', negateCondition: true, matchValue: ['POST'] }]
        }
        {
          name: 'RateLimitApi'
          priority: 20
          enabledState: 'Enabled'
          ruleType: 'RateLimitRule'
          rateLimitDurationInMinutes: 1
          rateLimitThreshold: rateLimitPerMinute
          action: 'Block'
          matchConditions: [{ matchVariable: 'RequestUri', operator: 'BeginsWith', matchValue: ['/'] }]
        }
      ]
    }
  }
}

resource securityPolicy 'Microsoft.Cdn/profiles/securityPolicies@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'api-waf'
  properties: {
    parameters: {
      type: 'WebApplicationFirewall'
      wafPolicy: { id: waf!.id }
      associations: [{ domains: [{ id: domain!.id }], patternsToMatch: ['/*'] }]
    }
  }
}

resource route 'Microsoft.Cdn/profiles/afdEndpoints/routes@2025-04-15' = if (deployEdge) {
  parent: endpoint
  name: 'api-only'
  properties: {
    enabledState: trafficState
    customDomains: [{ id: domain!.id }]
    originGroup: { id: originGroup!.id }
    supportedProtocols: ['Https']
    forwardingProtocol: 'HttpsOnly'
    httpsRedirect: 'Enabled'
    linkToDefaultDomain: 'Disabled'
    patternsToMatch: [
      '/chat/completions'
      '/v1/chat/completions'
      '/responses'
      '/v1/responses'
      '/embeddings'
      '/v1/embeddings'
    ]
    ruleSets: []
  }
  dependsOn: [origin, securityPolicy]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployEdge) {
  scope: profile
  name: 'edge-security-and-health'
  properties: {
    workspaceId: workspace.id
    logs: [for category in ['FrontDoorAccessLog', 'FrontDoorHealthProbeLog', 'FrontDoorWebApplicationFirewallLog']: { category: category, enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output edge object = {
  provisioned: deployEdge
  trafficEnabled: deployEdge && enableApiTraffic
  apiHost: apiHost
  adminHost: 'llm-admin.${baseDomain}'
  endpointHost: deployEdge ? endpoint!.properties.hostName : ''
  profileId: deployEdge ? profile!.properties.frontDoorId : ''
}
