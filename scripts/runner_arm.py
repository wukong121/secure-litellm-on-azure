"""Bounded ARM transport for an approved disposable private runner VM."""

import copy
import re
import time

from scripts.customer_migration import require
from scripts.runner_lifecycle import runner_snapshot, start_script


class RunnerArm:
    def __init__(self, machine_id, subnet_id, image_id, repository, environment, credential, session=None):
        import requests

        match = re.fullmatch(r"(/subscriptions/[a-fA-F0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+)/providers/Microsoft.Compute/virtualMachines/(llmgw-runner-[a-f0-9]{20})", machine_id)
        require(match is not None, "Runner VM is outside the managed naming contract")
        require(re.fullmatch(r"/subscriptions/[a-fA-F0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/Microsoft.Network/virtualNetworks/[A-Za-z0-9_.-]+/subnets/[A-Za-z0-9_.-]+", subnet_id), "Invalid approved runner subnet")
        require(re.fullmatch(r"/subscriptions/[a-fA-F0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/Microsoft.Compute/galleries/[A-Za-z0-9_.-]+/images/[A-Za-z0-9_.-]+/versions/[0-9]+\.[0-9]+\.[0-9]+", image_id), "Use an immutable approved runner image")
        require(environment in {"dev", "test", "prod"}, "Invalid runner environment")
        group, name = match.groups()
        self.machine_id, self.subnet_id, self.image_id = machine_id, subnet_id, image_id
        self.repository, self.environment, self.credential = repository, environment, credential
        self.nic_id = group + "/providers/Microsoft.Network/networkInterfaces/" + name + "-nic"
        self.nsg_id = group + "/providers/Microsoft.Network/networkSecurityGroups/" + name + "-nsg"
        self.disk_id = group + "/providers/Microsoft.Compute/disks/" + name + "-os"
        self.command_id = machine_id + "/runCommands/llmgw-job"
        self.versions = {machine_id: "2024-11-01", self.nic_id: "2024-07-01", self.nsg_id: "2024-07-01", self.disk_id: "2024-03-02", subnet_id: "2024-07-01", self.command_id: "2024-11-01"}
        self.session = session or requests.Session()
        self.session.trust_env = False

    def close(self):
        self.session.close()

    def request(self, method, resource, body=None, optional=False):
        require(resource in self.versions, "ARM runner operation is outside the exact resource allowlist")
        permitted = method == "GET" or (method == "PUT" and resource == self.command_id) or (method == "DELETE" and resource in {self.machine_id, self.nsg_id})
        require(permitted, "ARM operation is not permitted for this runner resource")
        try:
            token = self.credential.get_token("https://management.azure.com/.default").token
            response = self.session.request(method, "https://management.azure.com" + resource,
                                            params={"api-version": self.versions[resource]}, json=body,
                                            headers={"Authorization": "Bearer " + token}, timeout=60, allow_redirects=False)
            if optional and method == "GET" and response.status_code == 404:
                return None
            expected = {"GET": {200}, "PUT": {200, 201, 202}, "DELETE": {200, 202, 204}}[method]
            require(response.status_code in expected, "Runner ARM request failed")
            if method != "GET":
                return None
            value = response.json()
            require(isinstance(value, dict) and value.get("id", "").lower() == resource.lower(), "Runner ARM response resource mismatch")
            return value
        except Exception:
            raise ValueError("Runner ARM operation failed; response and credentials are suppressed") from None

    def snapshot(self, optional=False):
        machine = self.request("GET", self.machine_id, optional=optional)
        if machine is None:
            return None
        network = self.request("GET", self.nic_id)
        subnet = self.request("GET", self.subnet_id)
        security = self.request("GET", self.nsg_id)
        properties = machine["properties"]
        require(properties.get("provisioningState") == "Succeeded", "Runner VM is not provisioned")
        require(properties.get("securityProfile", {}).get("uefiSettings", {}).get("vTpmEnabled") is True, "Runner vTPM must be enabled")
        require(network["properties"].get("networkSecurityGroup", {}).get("id", "").lower() == self.nsg_id.lower(), "Runner NIC must attach its owned NSG")
        expected_rule = {"access": "Deny", "direction": "Inbound", "priority": 100, "protocol": "*", "sourceAddressPrefix": "*", "sourcePortRange": "*", "destinationAddressPrefix": "*", "destinationPortRange": "*"}
        rules = security["properties"].get("securityRules", [])
        require(len(rules) == 1 and all(rules[0].get("properties", {}).get(key) == value for key, value in expected_rule.items()), "Runner NSG must deny all inbound traffic")
        require(not security["properties"].get("subnets"), "Runner NSG must not be attached to a shared subnet")
        require(all(item.get("id", "").lower() == self.nic_id.lower() for item in security["properties"].get("networkInterfaces", [])), "Runner NSG is shared with another NIC")
        tags = {"managedBy": "llmgw-runner", "repository": self.repository, "environment": self.environment}
        require(all(security.get("tags", {}).get(key) == value for key, value in tags.items()), "Runner NSG ownership differs")
        disk = properties["storageProfile"]["osDisk"]
        require(disk.get("deleteOption") == "Delete" and disk.get("managedDisk", {}).get("id", "").lower() == self.disk_id.lower(), "Runner OS disk must be owned and deleted with the VM")
        require(properties["networkProfile"]["networkInterfaces"][0].get("properties", {}).get("deleteOption") == "Delete", "Runner NIC must be deleted with the VM")
        result = runner_snapshot(machine, network, subnet, self.repository, self.environment, self.image_id)
        result.update(diskId=self.disk_id, securityGroupId=self.nsg_id, routeTableId=subnet["properties"]["routeTable"]["id"],
                      securityProfile=copy.deepcopy(properties["securityProfile"]), location=machine["location"])
        return result

    def managed_command(self, machine_id, body):
        require(machine_id == self.machine_id, "Managed command cannot target another VM")
        properties = body.get("properties", {})
        require(set(body) == {"properties"} and set(properties) == {"source", "protectedParameters", "runAsUser", "timeoutInSeconds", "asyncExecution"}, "Unexpected managed command fields")
        require(properties["source"] == {"script": start_script()} and properties["runAsUser"] == "runner" and properties["timeoutInSeconds"] == 7560 and properties["asyncExecution"] is True, "Only the reviewed one-job startup script is permitted")
        require(len(properties["protectedParameters"]) == 1 and set(properties["protectedParameters"][0]) == {"name", "value"} and properties["protectedParameters"][0]["name"] == "LLMGW_JIT_CONFIG", "Only protected JIT configuration may be transmitted")
        require(self.request("GET", self.command_id, optional=True) is None, "A runner command already exists; do not replay registration material")
        snapshot = self.snapshot()
        self.request("PUT", self.command_id, {"location": snapshot["location"], **body})

    def wait_absent(self, resources, timeout=600):
        deadline = time.monotonic() + timeout
        remaining = set(resources)
        while remaining:
            remaining = {resource for resource in remaining if self.request("GET", resource, optional=True) is not None}
            if not remaining:
                return
            require(time.monotonic() < deadline, "Runner deletion is unconfirmed; preserve its cleanup checkpoint")
            time.sleep(2)

    def delete_machine(self, machine_id, vm_id):
        require(machine_id == self.machine_id, "Cleanup cannot target another VM")
        snapshot = self.snapshot(optional=True)
        if snapshot is not None:
            require(snapshot["vmId"] == vm_id, "Runner VM was replaced; refuse deletion")
            self.request("DELETE", self.machine_id)
        self.wait_absent({self.machine_id, self.nic_id, self.disk_id})
        security = self.request("GET", self.nsg_id, optional=True)
        if security is None:
            return
        tags = {"managedBy": "llmgw-runner", "repository": self.repository, "environment": self.environment}
        require(all(security.get("tags", {}).get(key) == value for key, value in tags.items()) and not security["properties"].get("networkInterfaces") and not security["properties"].get("subnets"), "Runner NSG ownership or attachments changed; preserve it")
        self.request("DELETE", self.nsg_id)
        self.wait_absent({self.nsg_id})