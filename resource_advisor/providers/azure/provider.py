from azure.mgmt.compute.aio import ComputeManagementClient
from resource_advisor.core.interfaces import CloudProvider
from resource_advisor.core.models import MetricSeries, ResourceConfig, SKUCandidate
from .resolver import resolve_resource
from .skus import list_available_skus
from .metrics import get_metrics

class AzureProvider(CloudProvider):
    def __init__(self, credential, default_subscription: str | None = None):
        self.credential = credential
        self.default_subscription = default_subscription

    async def resolve_resource(self, resource_url: str) -> dict:
        return await resolve_resource(resource_url, self.credential, self.default_subscription)

    async def get_metrics(self, resource: dict, lookback_days: int, percentile: float) -> dict[str, MetricSeries]:
        return await get_metrics(resource, lookback_days, self.credential, percentile)

    async def get_current_config(self, resource: dict) -> ResourceConfig:
        resource_type = resource["resource_type"].lower()

        if resource_type == "microsoft.compute/virtualmachines":
            client = ComputeManagementClient(self.credential, resource["subscription_id"])
            try:
                vm = await client.virtual_machines.get(resource["resource_group"], resource["name"])
                sku_name = vm.hardware_profile.vm_size
                region = resource.get("region", "eastus")

                cpu_cores = 0.0
                memory_gb = 0.0
                async for sku in client.resource_skus.list(filter=f"location eq '{region}'"):
                    if sku.name == sku_name and sku.resource_type == "virtualMachines":
                        for cap in (sku.capabilities or []):
                            if cap.name == "vCPUs":
                                cpu_cores = float(cap.value)
                            elif cap.name == "MemoryGB":
                                memory_gb = float(cap.value)
                        break

                return ResourceConfig(
                    sku_name=sku_name,
                    cpu_cores=cpu_cores,
                    memory_gb=memory_gb,
                )
            finally:
                await client.close()

        return ResourceConfig(
            sku_name="Unknown",
            cpu_cores=1.0,
            memory_gb=1.0,
        )

    async def list_available_skus(self, resource: dict, subscription_id: str) -> list[SKUCandidate]:
        return await list_available_skus(resource, subscription_id, self.credential)

    def supports(self, resource_url: str) -> bool:
        return "azure.com" in resource_url.lower() or resource_url.lower().startswith("/subscriptions/") or (
            self.default_subscription is not None and len(resource_url.split('/')) == 4
        )
