targetScope = 'resourceGroup'

param location string
param runnerName string
@description('Approved immutable Linux toolchain image version resource ID.')
param imageVersionResourceId string
param subnetResourceId string
param virtualMachineSize string
param sshPublicKey string
param adminUsername string = 'runner'
param repository string
param environmentName string
param tags object = {}

var ownedTags = union(tags, { managedBy: 'llmgw-runner', repository: repository, environment: environmentName })
resource security 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: '${runnerName}-nsg'
  location: location
  tags: ownedTags
  properties: {
    securityRules: [{
      name: 'deny-all-inbound'
      properties: {
        access: 'Deny'
        direction: 'Inbound'
        priority: 100
        protocol: '*'
        sourceAddressPrefix: '*'
        sourcePortRange: '*'
        destinationAddressPrefix: '*'
        destinationPortRange: '*'
      }
    }]
  }
}
resource nic 'Microsoft.Network/networkInterfaces@2024-07-01' = {
  name: '${runnerName}-nic'
  location: location
  tags: ownedTags
  properties: {
    enableIPForwarding: false
    networkSecurityGroup: { id: security.id }
    ipConfigurations: [{ name: 'private', properties: { privateIPAllocationMethod: 'Dynamic', subnet: { id: subnetResourceId } } }]
  }
}
resource machine 'Microsoft.Compute/virtualMachines@2024-11-01' = {
  name: runnerName
  location: location
  tags: ownedTags
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
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: { publicKeys: [{ path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: sshPublicKey }] }
      }
    }
    storageProfile: {
      imageReference: { id: imageVersionResourceId }
      osDisk: { name: '${runnerName}-os', createOption: 'FromImage', deleteOption: 'Delete', managedDisk: { storageAccountType: 'Premium_LRS' } }
      dataDisks: []
    }
    networkProfile: { networkInterfaces: [{ id: nic.id, properties: { primary: true, deleteOption: 'Delete' } }] }
    diagnosticsProfile: { bootDiagnostics: { enabled: false } }
  }
}
output privateRunner object = {
  id: machine.id
  name: machine.name
  nicId: nic.id
  nsgId: security.id
  imageVersionResourceId: imageVersionResourceId
  repository: repository
  environment: environmentName
  registered: false
}
