from abc import ABC, abstractmethod
from .models import MetricSeries, ResourceConfig, SKUCandidate

class CloudProvider(ABC):

    @abstractmethod
    async def resolve_resource(self, resource_url: str) -> dict:
        """
        Parse the URL/ID and return a normalised dict with:
        {
          "resource_id": str,      # canonical cloud resource ID
          "resource_type": str,    # e.g. "Microsoft.Compute/virtualMachines"
          "subscription_id": str,
          "resource_group": str,
          "region": str,
          "name": str
        }
        """

    @abstractmethod
    async def get_metrics(
        self,
        resource: dict,
        lookback_days: int,
        percentile: float,
    ) -> dict[str, MetricSeries]:
        """
        Fetch metric history for the resource.
        Returns a dict keyed by metric name.
        Which metrics are fetched depends on resource type
        (CPU + memory for VMs, DTU for SQL, etc.)
        """

    @abstractmethod
    async def get_current_config(self, resource: dict) -> ResourceConfig:
        """Return the currently provisioned spec and its cost."""

    @abstractmethod
    async def list_available_skus(
        self,
        resource: dict,
        subscription_id: str,
    ) -> list[SKUCandidate]:
        """
        Enumerate every SKU available for this resource type
        in this subscription + region, with pricing attached.
        """

    @abstractmethod
    def supports(self, resource_url: str) -> bool:
        """Return True if this provider can handle this URL format."""
