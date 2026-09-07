using './main.bicep'
param deployPrivateOrigin = false
param location = 'westus'
param apiLoadBalancer = {
  resourceGroupName: 'REPLACE_NEW_AKS_NODE_RESOURCE_GROUP'
  name: 'REPLACE_STANDARD_INTERNAL_API_LOAD_BALANCER'
  frontendName: 'REPLACE_PRIVATE_API_FRONTEND'
}
