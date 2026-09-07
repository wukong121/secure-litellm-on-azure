targetScope = 'resourceGroup'

@description('Azure region for the backup storage resources.')
param location string = resourceGroup().location

@description('Globally unique Storage Account name. The default is deterministic for this subscription and resource group.')
@minLength(3)
@maxLength(24)
param storageAccountName string = 'stlitellmpg${uniqueString(subscription().subscriptionId, resourceGroup().id)}'

@description('Dedicated LiteLLM security VNet name.')
param virtualNetworkName string = 'litellm-security-vnet'

@description('Dedicated LiteLLM security VNet address space. Confirm it does not overlap with enterprise/on-premises networks before deployment.')
param virtualNetworkAddressPrefix string = '10.30.0.0/16'

@description('Dedicated Private Endpoint subnet name.')
param privateEndpointSubnetName string = 'snet-private-endpoints'

@description('Dedicated Private Endpoint subnet prefix within the LiteLLM VNet.')
param privateEndpointSubnetPrefix string = '10.30.8.0/24'

@description('Existing Log Analytics workspace receiving Storage diagnostic logs.')
param logAnalyticsWorkspaceName string

@description('Microsoft Entra user object ID of the customer-approved backup owner. Do not infer it from the workflow identity.')
param backupOwnerPrincipalId string

@description('Human-readable owner identity recorded as resource metadata only.')
param backupOwnerUpn string

@description('Storage redundancy for the logical backup account.')
@allowed([
  'Standard_GRS'
  'Standard_GZRS'
  'Standard_RAGRS'
  'Standard_RAGZRS'
])
param storageSku string = 'Standard_GRS'

@description('Days that deleted blobs and containers remain recoverable after lifecycle deletion.')
@minValue(1)
@maxValue(365)
param softDeleteRetentionDays int = 14

@description('Resource tags applied to the backup resources.')
param tags object = {
  workload: 'litellm'
  dataClassification: 'confidential'
  purpose: 'postgresql-backup'
  owner: backupOwnerUpn
  managedBy: 'bicep'
  environment: 'current'
}

var backupContainerName = 'litellm-postgresql'
var blobPrivateDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'
var privateEndpointName = '${storageAccountName}-blob-pe'
var privateEndpointNetworkSecurityGroupName = 'nsg-litellm-private-endpoints'

// Built-in role definitions. No subscription or tenant identifiers are embedded in source.
var storageAccountContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '17d1049b-9a84-46fb-8f53-869881c3d3ab'
)
var storageBlobDataOwnerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
)

resource privateEndpointNetworkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: privateEndpointNetworkSecurityGroupName
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  name: virtualNetworkName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        virtualNetworkAddressPrefix
      ]
    }
    dhcpOptions: {
      dnsServers: []
    }
  }
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
  properties: {
    addressPrefix: privateEndpointSubnetPrefix
    networkSecurityGroup: {
      id: privateEndpointNetworkSecurityGroup.id
    }
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
  }
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: storageSku
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    dnsEndpointType: 'Standard'
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: true
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
      }
    }
    isHnsEnabled: false
    isLocalUserEnabled: false
    isSftpEnabled: false
    keyPolicy: {
      keyExpirationPeriodInDays: 90
    }
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    containerDeleteRetentionPolicy: {
      enabled: true
      days: softDeleteRetentionDays
      allowPermanentDelete: false
    }
    deleteRetentionPolicy: {
      enabled: true
      days: softDeleteRetentionDays
      allowPermanentDelete: false
    }
    isVersioningEnabled: true
  }
}

resource backupContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: backupContainerName
  properties: {
    defaultEncryptionScope: '$account-encryption-key'
    publicAccess: 'None'
    denyEncryptionScopeOverride: true
    metadata: {
      classification: 'confidential'
      workload: 'litellm-postgresql'
      owner: backupOwnerUpn
    }
  }
}

resource lifecyclePolicy 'Microsoft.Storage/storageAccounts/managementPolicies@2025-06-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          enabled: true
          name: 'delete-daily-after-14-days'
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 14
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${backupContainerName}/daily/'
              ]
            }
          }
        }
        {
          enabled: true
          name: 'delete-weekly-after-8-weeks'
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 56
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${backupContainerName}/weekly/'
              ]
            }
          }
        }
        {
          enabled: true
          name: 'delete-monthly-after-12-months'
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 365
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${backupContainerName}/monthly/'
              ]
            }
          }
        }
        {
          enabled: true
          name: 'delete-pre-change-after-90-days'
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 90
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${backupContainerName}/pre-change/'
              ]
            }
          }
        }
        {
          enabled: true
          name: 'delete-restore-test-after-1-day'
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 1
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${backupContainerName}/restore-test/'
              ]
            }
          }
        }
        {
          enabled: true
          name: 'delete-previous-versions-after-14-days'
          type: 'Lifecycle'
          definition: {
            actions: {
              version: {
                delete: {
                  daysAfterCreationGreaterThan: 14
                }
              }
              snapshot: {
                delete: {
                  daysAfterCreationGreaterThan: 14
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${backupContainerName}/'
              ]
            }
          }
        }
      ]
    }
  }
}

resource blobPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: blobPrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource blobPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobPrivateDnsZone
  name: '${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource blobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: privateEndpointName
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: '${privateEndpointName}-nic'
    privateLinkServiceConnections: [
      {
        name: '${storageAccountName}-blob-connection'
        properties: {
          groupIds: [
            'blob'
          ]
          privateLinkServiceId: storageAccount.id
        }
      }
    ]
    subnet: {
      id: privateEndpointSubnet.id
    }
  }
}

resource blobPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: blobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob-private-dns-zone'
        properties: {
          privateDnsZoneId: blobPrivateDnsZone.id
        }
      }
    ]
  }
}

resource storageDiagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: storageAccount
  name: 'send-storage-metrics-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspace.id
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

resource blobDiagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: blobService
  name: 'send-blob-audit-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspace.id
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

resource backupOwnerManagementRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, backupOwnerPrincipalId, storageAccountContributorRoleId)
  scope: storageAccount
  properties: {
    principalId: backupOwnerPrincipalId
    principalType: 'User'
    roleDefinitionId: storageAccountContributorRoleId
    description: 'Allows the designated LiteLLM PostgreSQL backup owner to manage this Storage Account.'
  }
}

resource backupOwnerDataRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, backupOwnerPrincipalId, storageBlobDataOwnerRoleId)
  scope: storageAccount
  properties: {
    principalId: backupOwnerPrincipalId
    principalType: 'User'
    roleDefinitionId: storageBlobDataOwnerRoleId
    description: 'Allows the designated LiteLLM PostgreSQL backup owner to read, write, restore, and delete backup blobs.'
  }
}

output backupStorage object = {
  storageAccountName: storageAccount.name
  containerName: backupContainer.name
  virtualNetworkName: virtualNetwork.name
  privateEndpointSubnetName: privateEndpointSubnet.name
  privateEndpointName: blobPrivateEndpoint.name
  privateDnsZoneName: blobPrivateDnsZone.name
  owner: backupOwnerUpn
  lifecyclePrefixes: {
    daily: '${backupContainerName}/daily/'
    weekly: '${backupContainerName}/weekly/'
    monthly: '${backupContainerName}/monthly/'
    preChange: '${backupContainerName}/pre-change/'
    restoreTest: '${backupContainerName}/restore-test/'
  }
}
