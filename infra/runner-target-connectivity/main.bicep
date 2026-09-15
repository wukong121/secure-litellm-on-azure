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

output runnerTargetConnectivity object = {
  aksResourceId: aksResourceId
  apiHostname: apiHostname
  runnerVirtualNetworkId: runnerVirtualNetworkId
  privateDnsZoneId: resourceId(dnsResourceGroupName, 'Microsoft.Network/privateDnsZones', privateDnsZoneName)
  linkName: linkName
  manageDnsLink: manageDnsLink
  linkManagedByDeployment: manageDnsLink && createDnsLink
  location: location
}
