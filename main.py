# -*- coding: utf-8 -*-
"""Azure SKU Recommendation CLI Tool"""
from __future__ import annotations
import warnings
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"azure\.mgmt")
warnings.filterwarnings("ignore", category=UserWarning, module=r"msal")
import re, sys, argparse
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, unquote
from typing import Any
import requests
from azure.identity import AzureCliCredential, InteractiveBrowserCredential, ChainedTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.web import WebSiteManagementClient
from azure.mgmt.sql import SqlManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.storage import StorageManagementClient
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()

# ── Constants ──────────────────────────────────────────────────────────────────
UNDER_THRESHOLD  = 0.20
OVER_THRESHOLD   = 0.80
ARTIFICIAL_RATIO = 3.0

# ── Resource ID Parser ─────────────────────────────────────────────────────────
def parse_resource_id(raw: str) -> dict:
    """Accept portal URL or raw resource ID and return components."""
    s = raw.strip()
    if s.startswith("http"):
        parsed = urlparse(s)
        # portal URL: fragment starts with /resource/subscriptions/...
        frag = unquote(parsed.fragment)
        match = re.search(r"/resource(/subscriptions/.+)", frag)
        if match:
            s = match.group(1)
        else:
            # try path
            match = re.search(r"(/subscriptions/.+)", parsed.path)
            if match:
                s = unquote(match.group(1))
    # normalise
    parts = [p for p in s.split("/") if p]
    # expected: subscriptions sub resourceGroups rg providers ns type name [sub-type sub-name]
    try:
        sub_idx   = next(i for i, p in enumerate(parts) if p.lower() == "subscriptions")
        rg_idx    = next(i for i, p in enumerate(parts) if p.lower() == "resourcegroups")
        prov_idx  = next(i for i, p in enumerate(parts) if p.lower() == "providers")
    except StopIteration:
        raise ValueError(f"Cannot parse resource ID from: {raw}")

    subscription_id = parts[sub_idx + 1]
    resource_group  = parts[rg_idx  + 1]
    provider        = parts[prov_idx + 1]
    resource_type   = parts[prov_idx + 2]
    resource_name   = parts[prov_idx + 3]

    # nested resource (e.g. servers/my-sql/databases/my-db)
    sub_type = sub_name = None
    if len(parts) > prov_idx + 5:
        sub_type = parts[prov_idx + 4]
        sub_name = parts[prov_idx + 5]

    resource_id = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/{provider}/{resource_type}/{resource_name}"
    if sub_type:
        resource_id += f"/{sub_type}/{sub_name}"

    return {
        "subscription_id": subscription_id,
        "resource_group":  resource_group,
        "provider":        provider.lower(),
        "resource_type":   resource_type.lower(),
        "resource_name":   resource_name,
        "sub_type":        sub_type.lower() if sub_type else None,
        "sub_name":        sub_name,
        "resource_id":     resource_id,
        "full_type":       f"{provider}/{resource_type}".lower() + (f"/{sub_type}".lower() if sub_type else ""),
    }


# ── Available VM SKU Helper (uses resource_skus API, not virtual_machine_sizes) ──
class VMSizeInfo:
    """Normalised VM size info built from ResourceSku capabilities."""
    __slots__ = ("name", "number_of_cores", "memory_in_mb")

    def __init__(self, name: str, vcpus: int, memory_mb: int):
        self.name          = name
        self.number_of_cores = vcpus
        self.memory_in_mb  = memory_mb

    @property
    def memory_gb(self): return self.memory_in_mb / 1024


def list_available_vm_sizes(compute_client, location: str) -> list[VMSizeInfo]:
    """
    Return VM sizes that are actually deployable (no restrictions) in the given region.
    Uses the same resource_skus API that 'az vm list-skus' uses — filters out
    any size with active restrictions in the subscription.
    """
    results = []
    try:
        skus = compute_client.resource_skus.list(filter=f"location eq '{location}'")
        for s in skus:
            if s.resource_type != "virtualMachines":
                continue
            # Skip if any restriction applies (e.g. NotAvailableForSubscription)
            if s.restrictions:
                continue
            caps = {c.name: c.value for c in (s.capabilities or [])}
            try:
                vcpus  = int(caps.get("vCPUs", 0))
                mem_gb = float(caps.get("MemoryGB", 0))
                if vcpus > 0 and mem_gb > 0:
                    results.append(VMSizeInfo(s.name, vcpus, int(mem_gb * 1024)))
            except (ValueError, TypeError):
                pass
    except Exception as e:
        console.print(f"[yellow]Warning: could not list available VM sizes: {e}[/yellow]")
    return results


# ── Metric Discovery & Collection ─────────────────────────────────────────────
AVG_AGGREGATION_TYPE = "Average"

def discover_metrics(monitor_client: MonitorManagementClient, resource_id: str) -> list[tuple[str, str, str]]:
    """
    Auto-discover all metrics available for a resource that support Average aggregation.
    Returns list of (metric_name, unit, display_name).
    """
    def _str(v) -> str:
        """Handle both plain strings and SDK enums that have a .value attribute."""
        return v.value if hasattr(v, "value") else str(v)

    discovered = []
    try:
        defs = monitor_client.metric_definitions.list(resource_uri=resource_id)
        for d in defs:
            supported_aggs = [_str(a).lower() for a in (d.supported_aggregation_types or [])]
            if AVG_AGGREGATION_TYPE.lower() in supported_aggs:
                name    = _str(d.name.value) if hasattr(d.name, "value") else _str(d.name)
                unit    = _str(d.unit) if d.unit else "Count"
                display = (d.name.localized_value if hasattr(d.name, "localized_value") else None) or name
                discovered.append((name, unit, display))
    except Exception as e:
        console.print(f"[yellow]Warning: could not discover metrics: {e}[/yellow]")
    return discovered


def collect_metrics(monitor_client: MonitorManagementClient, resource_id: str, metric_names: list[str]) -> dict:
    """Fetch metrics in batches of 20 (Azure Monitor API limit per call)."""
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    fmt   = "%Y-%m-%dT%H:%M:%SZ"
    timespan = f"{start.strftime(fmt)}/{now.strftime(fmt)}"

    BATCH = 20  # Azure Monitor limit
    result = {}
    for i in range(0, len(metric_names), BATCH):
        batch = metric_names[i:i + BATCH]
        try:
            resp = monitor_client.metrics.list(
                resource_uri=resource_id,
                timespan=timespan,
                interval="PT1H",
                metricnames=",".join(batch),
                aggregation="Average,Maximum,Minimum",
            )
            for metric in resp.value:
                values = []
                for ts in metric.timeseries:
                    for d in ts.data:
                        if d.average is not None:
                            values.append(d.average)
                if values:
                    result[metric.name.value] = {
                        "avg": sum(values) / len(values),
                        "max": max(values),
                        "min": min(values),
                        "timeseries": values,
                    }
                else:
                    result[metric.name.value] = {"avg": 0, "max": 0, "min": 0, "timeseries": []}
        except Exception as e:
            console.print(f"[yellow]Warning: metric batch error ({batch[0]}...): {e}[/yellow]")
    return result


# ── Baseline Calculation ───────────────────────────────────────────────────────
def compute_baseline(avg: float, capacity: float) -> float:
    return min(1.3 * avg, capacity)


# ── Rule Engine ────────────────────────────────────────────────────────────────
# Metrics where high value = healthy (100% = good). Skip overutil/underutil checks.
_SKIP_METRICS = frozenset({"availability", "successpercentage", "healthstate", "reachability"})

# Metrics where high value = LOW utilization (e.g. free memory). Evaluate as (cap - avg).
_INVERTED_METRICS = frozenset({"available memory bytes"})

def evaluate(metrics: dict, config: dict, metric_defs: list) -> tuple[str, str, float, float]:
    """Return (verdict, metric_name, utilization_ratio, baseline)."""
    worst_verdict = "NO_RECOMMENDATION"
    worst_metric  = ""
    worst_ratio   = 0.0
    worst_baseline = 0.0

    for mname, _unit, _display in metric_defs:
        if mname not in metrics:
            continue
        if mname.lower() in _SKIP_METRICS:
            continue
        m = metrics[mname]
        if not m["timeseries"] or m["max"] == 0:
            continue
        cap = config.get(mname, config.get("default_cap", 100.0))

        inverted = mname.lower() in _INVERTED_METRICS
        if inverted:
            # High available bytes = low usage. Effective utilization = cap - available.
            eff_avg = max(cap - m["avg"], 0)
            eff_max = max(cap - m["min"], 0)  # lowest available = peak usage
        else:
            eff_avg = m["avg"]
            eff_max = m["max"]

        baseline = compute_baseline(eff_avg, cap)
        if baseline == 0:
            continue

        ratio = eff_avg / baseline if baseline else 0

        if cap > ARTIFICIAL_RATIO * eff_max and eff_max > 0:
            verdict = "ARTIFICIAL_BASELINE"
        elif eff_avg > OVER_THRESHOLD * baseline:
            verdict = "OVERUTILIZED"
        elif eff_avg < UNDER_THRESHOLD * baseline:
            verdict = "UNDERUTILIZED"
        else:
            verdict = "NO_RECOMMENDATION"

        priority = {"ARTIFICIAL_BASELINE": 3, "OVERUTILIZED": 2, "UNDERUTILIZED": 1, "NO_RECOMMENDATION": 0}
        if priority[verdict] > priority[worst_verdict]:
            worst_verdict  = verdict
            worst_metric   = mname
            worst_ratio    = ratio
            worst_baseline = baseline

    return worst_verdict, worst_metric, worst_ratio, worst_baseline


# ── Cost Fetching (Azure Retail Prices API) ────────────────────────────────────
def fetch_price(sku_name: str, location: str, service_name: str = "Virtual Machines") -> float | None:
    try:
        # Normalize SKU for pricing API: e.g. "Standard_D4as_v4" -> "D4as v4"
        normalized_sku = sku_name
        if normalized_sku.startswith("Standard_"):
            normalized_sku = normalized_sku[len("Standard_"):]
        elif normalized_sku.startswith("Basic_"):
            normalized_sku = normalized_sku[len("Basic_"):]
        normalized_sku = normalized_sku.replace("_", " ")

        filter_str = (
            f"armRegionName eq '{location}' and "
            f"skuName eq '{normalized_sku}' and "
            f"serviceName eq '{service_name}' and "
            f"priceType eq 'Consumption'"
        )
        url = "https://prices.azure.com/api/retail/prices"
        r = requests.get(url, params={"$filter": filter_str}, timeout=10)
        items = r.json().get("Items", [])
        for item in items:
            if "windows" not in item.get("productName", "").lower():
                return item.get("retailPrice")
    except Exception as e:
        console.print(f"[dim]Pricing lookup failed ({sku_name}): {e}[/dim]")
    return None


# ── Handlers ───────────────────────────────────────────────────────────────────
class VMHandler:
    label = "Virtual Machine"
    price_service_name = "Virtual Machines"

    def get_config(self, client: ComputeManagementClient, rid: dict) -> dict:
        vm = client.virtual_machines.get(rid["resource_group"], rid["resource_name"], expand="instanceView")
        size = vm.hardware_profile.vm_size
        sizes = list(client.virtual_machine_sizes.list(self._location(client, rid)))
        for s in sizes:
            if s.name.lower() == size.lower():
                return {
                    "sku": size,
                    "Percentage CPU": 100.0,
                    "Available Memory Bytes": s.memory_in_mb * 1024 * 1024,
                    "cpu_cores": s.number_of_cores,
                    "memory_gb": s.memory_in_mb / 1024,
                    "default_cap": 100.0,
                }
        return {"sku": size, "Percentage CPU": 100.0, "default_cap": 100.0}

    def get_metric_definitions(self):
        return [
            ("Percentage CPU", "Percent", "CPU %"),
            ("Available Memory Bytes", "Bytes", "Available Memory"),
        ]

    def _location(self, client, rid):
        vm = client.virtual_machines.get(rid["resource_group"], rid["resource_name"])
        return vm.location

    def get_resource_location(self, client, rid):
        return self._location(client, rid)

    def list_skus(self, client: ComputeManagementClient, location: str):
        sizes = list_available_vm_sizes(client, location)
        console.print(f"[dim]Found {len(sizes)} VM sizes available in {location}.[/dim]")
        return sizes

    def map_sku_to_config(self, sku) -> dict:
        return {
            "sku": sku.name,
            "Percentage CPU": 100.0,
            "Available Memory Bytes": sku.memory_in_mb * 1024 * 1024,
            "cpu_cores": sku.number_of_cores,
            "memory_gb": sku.memory_in_mb / 1024,
        }


class AppServicePlanHandler:
    label = "App Service Plan"
    price_service_name = "Azure App Service"

    def get_config(self, client: WebSiteManagementClient, rid: dict) -> dict:
        plan = client.app_service_plans.get(rid["resource_group"], rid["resource_name"])
        workers = plan.sku.capacity or 1
        return {"sku": plan.sku.name, "CpuPercentage": 100.0, "MemoryPercentage": 100.0, "default_cap": 100.0, "workers": workers}

    def get_metric_definitions(self):
        return [("CpuPercentage", "Percent", "CPU %"), ("MemoryPercentage", "Percent", "Memory %")]

    def get_resource_location(self, client, rid):
        plan = client.app_service_plans.get(rid["resource_group"], rid["resource_name"])
        return plan.location

    def list_skus(self, client: WebSiteManagementClient, location: str):
        # Return static tier list; Azure doesn't have a simple SKU list API for ASP
        tiers = [
            {"name": "F1",  "cpu_cores": 1,  "memory_gb": 1},
            {"name": "B1",  "cpu_cores": 1,  "memory_gb": 1.75},
            {"name": "B2",  "cpu_cores": 2,  "memory_gb": 3.5},
            {"name": "B3",  "cpu_cores": 4,  "memory_gb": 7},
            {"name": "S1",  "cpu_cores": 1,  "memory_gb": 1.75},
            {"name": "S2",  "cpu_cores": 2,  "memory_gb": 3.5},
            {"name": "S3",  "cpu_cores": 4,  "memory_gb": 7},
            {"name": "P1v3","cpu_cores": 2,  "memory_gb": 8},
            {"name": "P2v3","cpu_cores": 4,  "memory_gb": 16},
            {"name": "P3v3","cpu_cores": 8,  "memory_gb": 32},
        ]
        class _S:
            def __init__(self, d): self.__dict__.update(d)
        return [_S(t) for t in tiers]

    def map_sku_to_config(self, sku) -> dict:
        return {"sku": sku.name, "CpuPercentage": 100.0, "MemoryPercentage": 100.0, "cpu_cores": sku.cpu_cores, "memory_gb": sku.memory_gb}


class WebAppHandler:
    """Delegates to the App Service Plan of the web app."""
    label = "Web App"
    price_service_name = "Azure App Service"

    def _get_plan_rid(self, client: WebSiteManagementClient, rid: dict) -> dict:
        site = client.web_apps.get(rid["resource_group"], rid["resource_name"])
        plan_id = site.server_farm_id
        parts = plan_id.split("/")
        return {**rid, "resource_name": parts[-1], "resource_group": parts[parts.index("resourceGroups")+1]}

    def get_config(self, client, rid):
        return AppServicePlanHandler().get_config(client, self._get_plan_rid(client, rid))

    def get_metric_definitions(self):
        return AppServicePlanHandler().get_metric_definitions()

    def get_resource_location(self, client, rid):
        site = client.web_apps.get(rid["resource_group"], rid["resource_name"])
        return site.location

    def list_skus(self, client, location):
        return AppServicePlanHandler().list_skus(client, location)

    def map_sku_to_config(self, sku):
        return AppServicePlanHandler().map_sku_to_config(sku)


class AKSHandler:
    label = "AKS Cluster"
    price_service_name = "Virtual Machines"  # AKS nodes are VMs; price by node VM SKU

    def get_config(self, client: ContainerServiceClient, rid: dict) -> dict:
        cluster = client.managed_clusters.get(rid["resource_group"], rid["resource_name"])
        # Use the first system/user node pool as the representative pool
        pool = cluster.agent_pool_profiles[0] if cluster.agent_pool_profiles else None
        vm_size = pool.vm_size if pool else "Unknown"
        node_count = pool.count or 1 if pool else 1

        # Resolve VM size capacity via Compute (stored on handler for list_skus)
        self._vm_size  = vm_size
        self._location = cluster.location
        self._node_count = node_count
        self._cred = client._config.credential
        self._sub  = rid["subscription_id"]

        # Try to get VM size details for cores/memory
        cpu_cores = "N/A"
        memory_gb = None
        try:
            from azure.mgmt.compute import ComputeManagementClient
            from azure.identity import AzureCliCredential, InteractiveBrowserCredential
            compute = ComputeManagementClient(
                client._config.credential,
                rid["subscription_id"],
            )
            sizes = list(compute.virtual_machine_sizes.list(cluster.location))
            for s in sizes:
                if s.name.lower() == vm_size.lower():
                    cpu_cores = s.number_of_cores
                    memory_gb = s.memory_in_mb / 1024
                    break
        except Exception:
            pass

        return {
            "sku": vm_size,
            "node_count": node_count,
            "cpu_cores": cpu_cores,
            "memory_gb": memory_gb,
            # caps for the rule engine (per-node percentages)
            "node_cpu_usage_percentage": 100.0,
            "node_memory_working_set_percentage": 100.0,
            "default_cap": 100.0,
        }

    def get_metric_definitions(self):
        return [
            ("node_cpu_usage_percentage",           "Percent", "Node CPU %"),
            ("node_memory_working_set_percentage",  "Percent", "Node Memory %"),
        ]

    def get_resource_location(self, client, rid):
        c = client.managed_clusters.get(rid["resource_group"], rid["resource_name"])
        return c.location

    def list_skus(self, client, location: str):
        """List VM sizes available in the region — these are the valid AKS node pool sizes."""
        try:
            from azure.mgmt.compute import ComputeManagementClient
            # Reuse credential + sub stored during get_config
            cred = getattr(self, "_cred", None) or client._config.credential
            sub  = getattr(self, "_sub",  None) or client._config.subscription_id
            compute = ComputeManagementClient(cred, sub)
            sizes = list_available_vm_sizes(compute, location)
            console.print(f"[dim]Found {len(sizes)} VM sizes available in {location}.[/dim]")
            return sizes
        except Exception as e:
            console.print(f"[yellow]Warning: could not list AKS node VM sizes: {e}[/yellow]")
            return []

    def map_sku_to_config(self, sku):
        return {}


class SQLDatabaseHandler:
    label = "SQL Database"
    price_service_name = "Azure SQL Database"

    def get_config(self, client: SqlManagementClient, rid: dict) -> dict:
        # resource_name = server, sub_name = database
        server = rid["resource_name"]
        db_name = rid["sub_name"] or rid["resource_name"]
        db = client.databases.get(rid["resource_group"], server, db_name)
        return {"sku": db.sku.name if db.sku else "Unknown", "dtu_consumption_percent": 100.0, "default_cap": 100.0}

    def get_metric_definitions(self):
        return [("dtu_consumption_percent", "Percent", "DTU %"), ("storage_percent", "Percent", "Storage %")]

    def get_resource_location(self, client, rid):
        server = rid["resource_name"]
        db_name = rid["sub_name"] or rid["resource_name"]
        db = client.databases.get(rid["resource_group"], server, db_name)
        return db.location

    def list_skus(self, client: SqlManagementClient, location: str):
        return []

    def map_sku_to_config(self, sku):
        return {}


class StorageAccountHandler:
    label = "Storage Account"
    price_service_name = None  # Storage pricing is per-GB/per-transaction, not a single hourly rate

    def get_config(self, client: StorageManagementClient, rid: dict) -> dict:
        acct = client.storage_accounts.get_properties(rid["resource_group"], rid["resource_name"])
        return {"sku": acct.sku.name, "UsedCapacity": 5 * 1024**4, "default_cap": 5 * 1024**4}  # 5TB default cap

    def get_metric_definitions(self):
        return [("UsedCapacity", "Bytes", "Used Capacity")]

    def get_resource_location(self, client, rid):
        acct = client.storage_accounts.get_properties(rid["resource_group"], rid["resource_name"])
        return acct.location

    def list_skus(self, client, location):
        return []

    def map_sku_to_config(self, sku):
        return {}


class GenericHandler:
    label = "Azure Resource"
    price_service_name = None

    def get_config(self, client, rid):
        return {"sku": "Unknown", "Percentage CPU": 100.0, "default_cap": 100.0}

    def get_metric_definitions(self):
        return [("Percentage CPU", "Percent", "CPU %")]

    def get_resource_location(self, client, rid):
        return "unknown"

    def list_skus(self, client, location):
        return []

    def map_sku_to_config(self, sku):
        return {}


HANDLER_REGISTRY = {
    "microsoft.compute/virtualmachines":            VMHandler,
    "microsoft.web/serverfarms":                    AppServicePlanHandler,
    "microsoft.web/sites":                          WebAppHandler,
    "microsoft.containerservice/managedclusters":   AKSHandler,
    "microsoft.sql/servers/databases":              SQLDatabaseHandler,
    "microsoft.storage/storageaccounts":            StorageAccountHandler,
}


def get_handler(full_type: str):
    return HANDLER_REGISTRY.get(full_type, GenericHandler)()


# ── SKU Selector ───────────────────────────────────────────────────────────────
def pick_sku(verdict: str, skus: list, metrics: dict, current_config: dict, metric_defs: list) -> Any | None:
    if not skus:
        return None

    def cpu_cores(s):
        return getattr(s, "cpu_cores", getattr(s, "number_of_cores", None)) or 0
    def mem_gb(s):
        return getattr(s, "memory_gb", None) or (getattr(s, "memory_in_mb", 0) / 1024) or 0

    current_cores = current_config.get("cpu_cores", 0)
    current_mem   = current_config.get("memory_gb", 0)
    # Treat unknown/string values as 0
    if not isinstance(current_cores, (int, float)):
        current_cores = 0
    if not isinstance(current_mem, (int, float)):
        current_mem = 0

    # Compute peak utilisation fraction across all metrics (0–1 scale)
    peak_fracs = []
    avg_fracs  = []
    for mname, _unit, _display in metric_defs:
        if mname not in metrics or not metrics[mname]["timeseries"]:
            continue
        m   = metrics[mname]
        cap = current_config.get(mname, current_config.get("default_cap", 100.0))
        if cap and cap > 0:
            peak_fracs.append(m["max"] / cap)
            avg_fracs.append(m["avg"] / cap)

    # If no usable fractions, bail
    if not peak_fracs:
        return None

    worst_peak_frac = max(peak_fracs)  # e.g. 0.15 means 15% of capacity at peak
    worst_avg_frac  = max(avg_fracs)

    if verdict == "UNDERUTILIZED":
        # min_cores = minimum cores required to handle 1.3× peak load
        # We want: min_cores <= new_cores < current_cores (pick largest that fits)
        min_cores = max(1.3 * worst_peak_frac * current_cores, 1) if current_cores else 1
        min_mem   = max(1.3 * worst_peak_frac * current_mem,   0.5) if current_mem else 0.5

        candidates = [s for s in skus
                      if (not current_cores or cpu_cores(s) >= min_cores)
                      and (not current_mem   or mem_gb(s)   >= min_mem)
                      # must actually be smaller than what we have now
                      and (not current_cores or cpu_cores(s) < current_cores)
                      and (not current_mem   or mem_gb(s)   < current_mem or cpu_cores(s) < current_cores)]
        # Pick the largest (gives most headroom while still downsizing)
        candidates = sorted(candidates, key=lambda s: cpu_cores(s), reverse=True)
        return candidates[0] if candidates else None


    elif verdict == "OVERUTILIZED":
        # Target: smallest SKU ≥ 1.5× current average usage
        need_cores = 1.5 * worst_avg_frac * current_cores if current_cores else 0
        need_mem   = 1.5 * worst_avg_frac * current_mem   if current_mem   else 0
        candidates = [s for s in skus
                      if (not current_cores or cpu_cores(s) >= need_cores)
                      and (not current_mem   or mem_gb(s)   >= need_mem)]
        # Must be larger than current
        candidates = [s for s in candidates
                      if cpu_cores(s) > current_cores or mem_gb(s) > current_mem]
        candidates = sorted(candidates, key=lambda s: cpu_cores(s))
        return candidates[0] if candidates else None

    elif verdict == "ARTIFICIAL_BASELINE":
        # Right-size to 1.2× actual peak (not configured capacity)
        need_cores = max(1.2 * worst_peak_frac * current_cores, 1) if current_cores else 1
        need_mem   = max(1.2 * worst_peak_frac * current_mem,   0.5) if current_mem else 0.5
        candidates = [s for s in skus
                      if cpu_cores(s) >= need_cores and mem_gb(s) >= need_mem]
        # Must be smaller than current (it's an over-provisioned resource)
        candidates = [s for s in candidates
                      if cpu_cores(s) < current_cores or mem_gb(s) < current_mem]
        candidates = sorted(candidates, key=lambda s: cpu_cores(s))
        return candidates[0] if candidates else None

    return None



# ── Credential Helper ──────────────────────────────────────────────────────────
def _get_credential():
    """Try AzureCliCredential (cached az login) first; fall back to browser login."""
    try:
        cli_cred = AzureCliCredential()
        # probe with a short-lived token request to verify it works
        cli_cred.get_token("https://management.azure.com/.default")
        console.print("[dim]Using cached Azure CLI credentials.[/dim]")
        return cli_cred
    except Exception:
        console.print("[dim]No cached CLI credentials found — opening browser for login...[/dim]")
        return InteractiveBrowserCredential()


# ── Output ─────────────────────────────────────────────────────────────────────
VERDICT_COLOR = {
    "OVERUTILIZED":       "bold red",
    "UNDERUTILIZED":      "bold yellow",
    "ARTIFICIAL_BASELINE":"bold magenta",
    "NO_RECOMMENDATION":  "bold green",
}
VERDICT_ICON = {
    "OVERUTILIZED":       "[red][OVER][/red]",
    "UNDERUTILIZED":      "[yellow][UNDER][/yellow]",
    "ARTIFICIAL_BASELINE":"[magenta][INFLATED][/magenta]",
    "NO_RECOMMENDATION":  "[green][OK][/green]",
}


def render_output(rid: dict, handler, config: dict, metrics: dict, metric_defs: list,
                  all_metric_defs: list,
                  verdict: str, worst_metric: str, recommended_sku, location: str,
                  current_price: float | None, rec_price: float | None):
    color = VERDICT_COLOR.get(verdict, "white")
    icon  = VERDICT_ICON.get(verdict, "")

    lines = []
    lines.append(f"[bold]Resource   :[/bold] {rid['resource_name']}  ([dim]{handler.label}[/dim])")
    lines.append(f"[bold]Region     :[/bold] {location}")
    mem_gb = config.get('memory_gb', None)
    cpu_cores = config.get('cpu_cores', None)
    if cpu_cores is not None or isinstance(mem_gb, (int, float)):
        mem_str = f"{mem_gb:.1f} GB RAM" if isinstance(mem_gb, (int, float)) else "N/A"
        lines.append(f"[bold]Current SKU:[/bold] {config.get('sku','?')}  "
                     f"([dim]{cpu_cores} vCPU, {mem_str}[/dim])")
    else:
        lines.append(f"[bold]Current SKU:[/bold] {config.get('sku','?')}")

    # ── Primary metrics (drive the rule engine) ────────────────────────────────
    primary_names = {m for m, *_ in metric_defs}
    lines.append("")
    lines.append("[bold underline]Primary Metrics (Rule Engine)[/bold underline]")
    primary_shown = False
    for mname, unit, display in metric_defs:
        if mname in metrics and metrics[mname]["timeseries"]:
            m = metrics[mname]
            cap = config.get(mname, config.get("default_cap", 100))
            baseline = compute_baseline(m["avg"], cap)
            unit_str = "%" if unit == "Percent" else (" GB" if unit == "Bytes" else "")
            scale = 1/1024**3 if unit == "Bytes" else 1
            lines.append(f"  {display:<28} Avg: {m['avg']*scale:>8.2f}{unit_str}  "
                         f"Peak: {m['max']*scale:>8.2f}{unit_str}  Baseline: {baseline*scale:>8.2f}{unit_str}")
            primary_shown = True
    if not primary_shown:
        lines.append("  [dim](no data — check monitoring is enabled)[/dim]")

    # ── All discovered metrics ──────────────────────────────────────────────────
    extra_defs = [(mn, u, dn) for mn, u, dn in all_metric_defs
                  if mn not in primary_names and mn in metrics and metrics[mn]["timeseries"]]
    if extra_defs:
        lines.append("")
        lines.append("[bold underline]All Discovered Metrics[/bold underline]")
        for mname, unit, display in extra_defs:
            m = metrics[mname]
            unit_str = "%" if unit == "Percent" else (" GB" if unit in ("Bytes", "ByteSeconds") else (" ms" if unit == "MilliSeconds" else ""))
            scale = 1/1024**3 if unit in ("Bytes", "ByteSeconds") else 1
            lines.append(f"  {display:<40} Avg: {m['avg']*scale:>10.2f}{unit_str}  Peak: {m['max']*scale:>10.2f}{unit_str}")

    lines.append("")
    lines.append(f"[bold underline]Verdict[/bold underline]")
    lines.append(f"  {icon} [{color}]{verdict}[/{color}]  (driven by [italic]{worst_metric}[/italic])")

    lines.append("")
    lines.append("[bold underline]Recommendation[/bold underline]")

    def _cores(s): return getattr(s, "cpu_cores", getattr(s, "number_of_cores", "?"))
    def _mem(s):   return getattr(s, "memory_gb", None) or (getattr(s, "memory_in_mb", 0) / 1024)

    cur_cores = config.get("cpu_cores", None)
    cur_mem   = config.get("memory_gb", None)
    cur_mem_str = f"{cur_mem:.1f} GB" if isinstance(cur_mem, (int, float)) else None
    cur_sku   = config.get("sku", "?")
    has_compute = cur_cores is not None or cur_mem_str is not None

    # ── Current resource row ────────────────────────────────────────────────────
    if handler.price_service_name:
        cur_price_str = f"${current_price:.4f}/hr" if current_price else "price N/A"
    else:
        cur_price_str = None  # pricing model not applicable (per-GB, per-transaction, etc.)

    cur_detail_parts = []
    if has_compute:
        cur_detail_parts.append(f"{cur_cores} vCPU · {cur_mem_str or 'N/A'} RAM")
    if cur_price_str:
        cur_detail_parts.append(cur_price_str)
    cur_detail = f"  [dim]{' · '.join(cur_detail_parts)}[/dim]" if cur_detail_parts else ""
    lines.append(f"  [bold]Current  :[/bold] [cyan]{cur_sku}[/cyan]{cur_detail}")

    if recommended_sku:
        rec_cores   = _cores(recommended_sku)
        rec_mem_val = _mem(recommended_sku)
        rec_mem_str = f"{rec_mem_val:.1f} GB" if rec_mem_val else None

        rec_detail_parts = []
        if has_compute:
            rec_detail_parts.append(f"{rec_cores} vCPU · {rec_mem_str or 'N/A'} RAM")
        if handler.price_service_name:
            rec_detail_parts.append(f"${rec_price:.4f}/hr" if rec_price else "price N/A")
        rec_detail = f"  [dim]{' · '.join(rec_detail_parts)}[/dim]" if rec_detail_parts else ""

        # ── Recommended resource row ────────────────────────────────────────────
        lines.append(f"  [bold]Recommended:[/bold] [bold green]{recommended_sku.name}[/bold green]{rec_detail}")

        # ── Cost delta ─────────────────────────────────────────────────────────
        lines.append("")
        if not handler.price_service_name:
            lines.append("  [dim]Pricing not shown — billing model is per-GB/per-transaction, not per-hour.[/dim]")
        elif current_price and rec_price:
            savings     = current_price - rec_price
            savings_pct = savings / current_price * 100
            monthly_savings = savings * 24 * 30
            if savings > 0:
                lines.append(f"  [bold green]↓ Saves ${savings:.4f}/hr  (~${monthly_savings:.0f}/month · {savings_pct:.0f}% cheaper)[/bold green]")
            else:
                lines.append(f"  [bold red]↑ Costs ${-savings:.4f}/hr more  (~${-monthly_savings:.0f}/month · {-savings_pct:.0f}% more expensive)[/bold red]")
        elif current_price:
            lines.append(f"  [dim]Current: ${current_price:.4f}/hr — recommended pricing unavailable[/dim]")
        else:
            lines.append("  [dim]Pricing lookup returned no results for this region/SKU. Run with --verbose to debug.[/dim]")

    elif verdict == "NO_RECOMMENDATION":
        lines.append(f"  [bold green]✓ No change needed[/bold green] — current SKU is appropriately sized.")
        if current_price:
            monthly = current_price * 24 * 30
            lines.append(f"  [dim]Current cost: ${current_price:.4f}/hr  (~${monthly:.0f}/month)[/dim]")
    else:
        if verdict in ("UNDERUTILIZED", "ARTIFICIAL_BASELINE"):
            lines.append("  [yellow]You are already on the smallest available SKU that fits the criteria in this region.[/yellow]")
        elif verdict == "OVERUTILIZED":
            lines.append("  [yellow]You are already on the largest available SKU in this region, or no larger SKU fits the criteria.[/yellow]")
        else:
            lines.append("  [yellow]No smaller/larger SKU found in this region matching the criteria.[/yellow]")
        lines.append("  [dim]Try running with --verbose to inspect metric values.[/dim]")
        if current_price:
            lines.append(f"  [dim]Current cost: ${current_price:.4f}/hr[/dim]")

    panel_text = "\n".join(lines)
    console.print(Panel(panel_text, title="[bold cyan]Azure SKU Recommendation[/bold cyan]", box=box.ROUNDED, padding=(1, 2)))



# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Azure SKU Recommendation Tool")
    parser.add_argument("resource_url", nargs="?", help="Azure resource URL or resource ID")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show raw metric timeseries")
    args = parser.parse_args()

    if args.resource_url:
        raw = args.resource_url
    else:
        console.print("[bold cyan]Azure SKU Recommendation Tool[/bold cyan]")
        raw = console.input("[bold]Paste Azure resource URL or ID:[/bold] ").strip()

    try:
        rid = parse_resource_id(raw)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(f"\n[dim]Parsed resource:[/dim] {rid['full_type']} / [bold]{rid['resource_name']}[/bold]")

    cred = _get_credential()
    sub  = rid["subscription_id"]

    handler = get_handler(rid["full_type"])

    # Build appropriate management client
    console.print("[dim]Connecting to Azure...[/dim]")
    if isinstance(handler, (VMHandler,)):
        mgmt = ComputeManagementClient(cred, sub)
    elif isinstance(handler, (AppServicePlanHandler, WebAppHandler)):
        mgmt = WebSiteManagementClient(cred, sub)
    elif isinstance(handler, AKSHandler):
        mgmt = ContainerServiceClient(cred, sub)
    elif isinstance(handler, SQLDatabaseHandler):
        mgmt = SqlManagementClient(cred, sub)
    elif isinstance(handler, StorageAccountHandler):
        mgmt = StorageManagementClient(cred, sub)
    else:
        mgmt = None

    monitor = MonitorManagementClient(cred, sub)

    with console.status("[bold green]Fetching resource config..."):
        try:
            config = handler.get_config(mgmt, rid)
        except Exception as e:
            console.print(f"[yellow]Warning: could not fetch config ({e}), using defaults.[/yellow]")
            config = {"sku": "Unknown", "default_cap": 100.0}

    with console.status("[bold green]Fetching location..."):
        try:
            location = handler.get_resource_location(mgmt, rid)
        except Exception as e:
            console.print(f"[yellow]Warning: could not fetch location ({e})[/yellow]")
            location = "eastus"

    metric_defs = handler.get_metric_definitions()
    primary_names = [m for m, *_ in metric_defs]

    with console.status("[bold green]Discovering all available metrics..."):
        all_metric_defs = discover_metrics(monitor, rid["resource_id"])
        console.print(f"[dim]Found {len(all_metric_defs)} available metric(s) for this resource.[/dim]")

    # Collect primary metrics + all discovered metrics (deduplicated)
    all_names = list(dict.fromkeys(
        primary_names + [m for m, *_ in all_metric_defs]
    ))

    with console.status(f"[bold green]Collecting 7-day metrics ({len(all_names)} metric(s) in batches)..."):
        metrics = collect_metrics(monitor, rid["resource_id"], all_names)

    # Check if all returned metrics have zero/empty timeseries (e.g. Container Insights disabled)
    has_real_data = any(v["timeseries"] for v in metrics.values())
    if not metrics or not has_real_data:
        console.print("[red]No metric data returned.[/red]")
        if isinstance(handler, AKSHandler):
            console.print("[yellow]Tip: AKS metrics (node_cpu_usage_percentage, node_memory_working_set_percentage) require "
                          "[bold]Azure Monitor Container Insights[/bold] to be enabled on the cluster.\n"
                          "Enable it via: Azure Portal → your AKS cluster → Insights → Enable[/yellow]")
        sys.exit(1)

    if args.verbose:
        console.print("\n[dim]--- Raw metric data ---[/dim]")
        for k, v in metrics.items():
            pts = len(v['timeseries'])
            console.print(f"  [bold]{k}[/bold]: avg={v['avg']:.4f} max={v['max']:.4f} points={pts}" +
                          (" [dim](no data)[/dim]" if pts == 0 else ""))

    verdict, worst_metric, ratio, baseline = evaluate(metrics, config, metric_defs)

    with console.status("[bold green]Fetching available SKUs..."):
        try:
            skus = handler.list_skus(mgmt, location)
        except Exception as e:
            console.print(f"[yellow]Warning: could not list SKUs ({e})[/yellow]")
            skus = []

    recommended_sku = pick_sku(verdict, skus, metrics, config, metric_defs)

    # Prices
    current_price = rec_price = None
    if config.get("sku") and config["sku"] != "Unknown" and handler.price_service_name:
        with console.status("[bold green]Fetching pricing data..."):
            current_price = fetch_price(config["sku"], location, handler.price_service_name)
            if recommended_sku:
                rec_price = fetch_price(recommended_sku.name, location, handler.price_service_name)

    render_output(rid, handler, config, metrics, metric_defs, all_metric_defs,
                  verdict, worst_metric, recommended_sku, location,
                  current_price, rec_price)


if __name__ == "__main__":
    main()
