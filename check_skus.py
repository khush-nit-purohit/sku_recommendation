import warnings; warnings.filterwarnings('ignore')
from azure.identity import AzureCliCredential
from azure.mgmt.compute import ComputeManagementClient

cred = AzureCliCredential()
compute = ComputeManagementClient(cred, 'aef8d17c-557e-41f2-ad90-e8f4015ddb63')

skus = list(compute.resource_skus.list(filter="location eq 'eastus'"))
vm_skus = [s for s in skus if s.resource_type == 'virtualMachines']
available = [s for s in vm_skus if not s.restrictions]

print(f'Total VM skus: {len(vm_skus)}, Unrestricted: {len(available)}')

# Show capabilities of one SKU
s = next((x for x in available if x.name == 'Standard_D2as_v4'), available[0])
caps = {c.name: c.value for c in (s.capabilities or [])}
print(f'Name: {s.name}')
print(f'vCPUs: {caps.get("vCPUs")}, MemoryGB: {caps.get("MemoryGB")}')
print(f'Sample cap keys: {list(caps.keys())}')
