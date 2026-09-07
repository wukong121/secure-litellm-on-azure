@description('Globally unique Azure Container Registry name.')
@minLength(5)
@maxLength(50)
param name string

@description('Azure region for the registry.')
param location string

@description('Deployment environment tag.')
@allowed([
  'dev'
  'test'
  'prod'
])
param environmentName string

@description('Allow public network access only for an explicitly approved bootstrap window.')
param publicNetworkAccess string = 'Disabled'

@description('Resource tags.')
param tags object = {}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  tags: union(tags, {
    environment: environmentName
    workload: 'litellm'
    managedBy: 'bicep'
  })
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    encryption: {
      status: 'disabled'
    }
    networkRuleBypassOptions: 'None'
    policies: {
      exportPolicy: {
        status: 'disabled'
      }
      quarantinePolicy: {
        status: 'disabled'
      }
      retentionPolicy: {
        days: 7
        status: 'enabled'
      }
      trustPolicy: {
        status: 'disabled'
        type: 'Notary'
      }
    }
    publicNetworkAccess: publicNetworkAccess
    zoneRedundancy: 'Disabled'
  }
}

output registry object = {
  id: registry.id
  name: registry.name
  loginServer: registry.properties.loginServer
}
