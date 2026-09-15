targetScope = 'resourceGroup'

param location string = resourceGroup().location
param tags object = {}
param runnerVirtualNetworkId string
param manageDnsLink bool = true
param createDnsLink bool = false
param dnsResourceGroupName string = resourceGroup().name
param privateDnsZoneName string = ''
param linkName string = ''
param aksResourceId string = ''
param apiHostname string = ''
param createAcrDnsLink bool = false
param acrDnsResourceGroupName string = resourceGroup().name
param acrPrivateDnsZoneName string = ''
param acrLinkName string = ''
param acrResourceId string = ''
param acrLoginServer string = ''

module runnerLink 'dns-link.bicep' = if (manageDnsLink && createDnsLink) {
  name: linkName
  scope: resourceGroup(dnsResourceGroupName)
  params: {
    privateDnsZoneName: privateDnsZoneName
    runnerVirtualNetworkId: runnerVirtualNetworkId
    linkName: linkName
    tags: tags
  }
}

module acrRunnerLink 'dns-link.bicep' = if (manageDnsLink && createAcrDnsLink) {
  name: acrLinkName
  scope: resourceGroup(acrDnsResourceGroupName)
  params: {
    privateDnsZoneName: acrPrivateDnsZoneName
    runnerVirtualNetworkId: runnerVirtualNetworkId
    linkName: acrLinkName
    tags: tags
  }
}

output runnerTargetConnectivity object = {
  aksResourceId: aksResourceId
  apiHostname: apiHostname
  runnerVirtualNetworkId: runnerVirtualNetworkId
  privateDnsZoneId: resourceId(dnsResourceGroupName, 'Microsoft.Network/privateDnsZones', privateDnsZoneName)
  linkName: linkName
  manageDnsLink: manageDnsLink
  linkManagedByDeployment: manageDnsLink && createDnsLink
  acrResourceId: acrResourceId
  acrLoginServer: acrLoginServer
  acrPrivateDnsZoneId: resourceId(acrDnsResourceGroupName, 'Microsoft.Network/privateDnsZones', acrPrivateDnsZoneName)
  acrLinkName: acrLinkName
  acrLinkManagedByDeployment: manageDnsLink && createAcrDnsLink
  location: location
}
