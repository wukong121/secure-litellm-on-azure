targetScope = 'resourceGroup'

@description('Existing Azure AI/OpenAI account name.')
param accountName string

@description('LiteLLM workload identity principal ID.')
param principalId string

var cognitiveServicesOpenAIUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

resource modelDataPlaneRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, cognitiveServicesOpenAIUserRoleId)
  scope: account
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: cognitiveServicesOpenAIUserRoleId
    description: 'Allows only the LiteLLM workload identity to invoke this Azure OpenAI resource.'
  }
}

output roleAssignment object = {
  accountName: account.name
  roleAssignmentId: modelDataPlaneRole.id
}
