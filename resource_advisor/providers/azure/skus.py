import sqlite3
from typing import Optional

from azure.mgmt.compute.aio import ComputeManagementClient

from resource_advisor.core.models import SKUCandidate

from .pricing import enrich_skus_with_pricing

# Optional persistent DB connection — set via set_db_conn() from app startup or main.py
_db_conn: Optional[sqlite3.Connection] = None


def set_db_conn(conn: sqlite3.Connection):
    global _db_conn
    _db_conn = conn


async def list_available_skus(resource: dict, subscription_id: str, credential) -> list[SKUCandidate]:
    resource_type = resource["resource_type"].lower()
    region = resource["region"]

    candidates = []

    if resource_type == "microsoft.compute/virtualmachines":
        # Use DB cache when available; fall back to live API
        if _db_conn is not None:
            from resource_advisor.db.queries import get_vm_skus
            rows = get_vm_skus(_db_conn, subscription_id, region)
            if rows:
                for r in rows:
                    candidates.append(SKUCandidate(
                        sku_name=r["sku_name"],
                        cpu_cores=float(r["vcpus"]),
                        memory_gb=float(r["memory_mb"]) / 1024,
                        monthly_cost_usd=0.0,
                        cost_delta_usd=0.0,
                        cost_delta_pct=0.0,
                        clears_baseline=False,
                        region=region,
                    ))

        if not candidates:
            # Live API fallback
            client = ComputeManagementClient(credential, subscription_id)
            skus = []
            async for sku in client.resource_skus.list(filter=f"location eq '{region}'"):
                skus.append(sku)

            for sku in skus:
                if sku.resource_type != "virtualMachines":
                    continue
                if sku.locations and sku.location_info:
                    is_available = any(info.location.lower() == region.lower() for info in sku.location_info)
                    if not is_available:
                        continue

                cpu_cores = 0
                memory_gb = 0
                for capability in (sku.capabilities or []):
                    if capability.name == "vCPUs":
                        cpu_cores = float(capability.value)
                    elif capability.name == "MemoryGB":
                        memory_gb = float(capability.value)

                if cpu_cores > 0 and memory_gb > 0:
                    candidates.append(SKUCandidate(
                        sku_name=sku.name,
                        cpu_cores=cpu_cores,
                        memory_gb=memory_gb,
                        monthly_cost_usd=0.0,
                        cost_delta_usd=0.0,
                        cost_delta_pct=0.0,
                        clears_baseline=False,
                        region=region,
                    ))

    await enrich_skus_with_pricing(candidates, region, "Virtual Machines")

    return [c for c in candidates if c.monthly_cost_usd > 0 and c.monthly_cost_usd < 999999.0]
