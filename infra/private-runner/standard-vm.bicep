targetScope = 'resourceGroup'

param location string
param runnerName string
param subnetResourceId string
param sshPublicKey string
param sshSourceCidr string
param virtualMachineSize string
param adminUsername string
@allowed(['dev', 'test', 'prod'])
param environmentName string
param osDiskSizeGiB int
param dataDiskSizeGiB int
param ubuntuImageVersion string
param tags object

resource security 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: '${runnerName}-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: concat(empty(sshSourceCidr) ? [] : [
      {
        name: 'ssh-from-approved-management'
        properties: {
          access: 'Allow'
          direction: 'Inbound'
          priority: 100
          protocol: 'Tcp'
          sourceAddressPrefix: sshSourceCidr
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ], [
      {
        name: 'deny-all-other-inbound'
        properties: {
          access: 'Deny'
          direction: 'Inbound'
          priority: 110
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ])
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-07-01' = {
  name: '${runnerName}-nic'
  location: location
  tags: tags
  properties: {
    enableIPForwarding: false
    networkSecurityGroup: { id: security.id }
    ipConfigurations: [{
      name: 'private'
      properties: {
        privateIPAllocationMethod: 'Dynamic'
        subnet: { id: subnetResourceId }
      }
    }]
  }
}

resource workDisk 'Microsoft.Compute/disks@2024-03-02' = {
  name: '${runnerName}-work'
  location: location
  tags: tags
  sku: { name: 'Premium_LRS' }
  properties: {
    creationData: { createOption: 'Empty' }
    diskSizeGB: dataDiskSizeGiB
    encryption: { type: 'EncryptionAtRestWithPlatformKey' }
    networkAccessPolicy: 'DenyAll'
    publicNetworkAccess: 'Disabled'
  }
}

resource machine 'Microsoft.Compute/virtualMachines@2024-11-01' = {
  name: runnerName
  location: location
  tags: tags
  properties: {
    hardwareProfile: { vmSize: virtualMachineSize }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
      encryptionAtHost: true
    }
    osProfile: {
      computerName: runnerName
      adminUsername: adminUsername
      customData: base64(replace(loadTextContent('bootstrap.sh'), '__LLMGW_ENVIRONMENT__', environmentName))
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: { publicKeys: [{ path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: sshPublicKey }] }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: ubuntuImageVersion
      }
      osDisk: {
        name: '${runnerName}-os'
        createOption: 'FromImage'
        diskSizeGB: osDiskSizeGiB
        deleteOption: 'Detach'
        managedDisk: { storageAccountType: 'Premium_LRS' }
      }
      dataDisks: [{
        lun: 0
        name: workDisk.name
        createOption: 'Attach'
        managedDisk: { id: workDisk.id }
        caching: 'None'
        deleteOption: 'Detach'
      }]
    }
    networkProfile: { networkInterfaces: [{ id: nic.id, properties: { primary: true, deleteOption: 'Delete' } }] }
    diagnosticsProfile: { bootDiagnostics: { enabled: true } }
  }
}

output privateIpAddress string = nic.properties.ipConfigurations[0].properties.privateIPAddress
output virtualMachineId string = machine.id
