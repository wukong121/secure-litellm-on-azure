@description('User-assigned managed identity name for LiteLLM pods.')
param identityName string

@description('Azure region for the identity.')
param location string

@description('OIDC issuer URL from the target AKS cluster.')
param oidcIssuerUrl string

@description('Kubernetes namespace containing LiteLLM.')
param kubernetesNamespace string = 'litellm'

@description('Kubernetes ServiceAccount bound to the identity.')
param serviceAccountName string = 'litellm'

@description('Resource tags.')
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource federatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'aks-${kubernetesNamespace}-${serviceAccountName}'
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${kubernetesNamespace}:${serviceAccountName}'
  }
}

output workloadIdentity object = {
  id: identity.id
  name: identity.name
  clientId: identity.properties.clientId
  principalId: identity.properties.principalId
  serviceAccountName: serviceAccountName
  kubernetesNamespace: kubernetesNamespace
}
