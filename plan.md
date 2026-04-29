# Plan: Azure SKU Recommendation Script

## Context
Build a CLI tool that ingests an Azure resource URL, analyzes 7-day metric data, computes a usage baseline, and recommends SKU changes (downsize, upsize, or none) based on rule-based thresholds. Supports any Azure resource type via a handler registry.

---

## Files to Modify
- `main.py` — full rewrite (currently stub)
- `pyproject.toml` — add dependencies

---

## Dependencies (pyproject.toml)
```
azure-identity
azure-mgmt-monitor
azure-mgmt-compute
azure-mgmt-web
azure-mgmt-sql
azure-mgmt-containerservice
azure-mgmt-resource
azure-mgmt-storage
rich
```

---

## Architecture (single file `main.py`)

### 1. Resource ID Parsing
Parse Azure portal URLs and raw resource IDs:
- Portal: `https://portal.azure.com/#resource/subscriptions/{sub}/resourceGroups/{rg}/providers/{ns}/{type}/{name}`
- Raw ID: `/subscriptions/{sub}/resourceGroups/{rg}/providers/{ns}/{type}/{name}`

Extract: `subscription_id`, `resource_group`, `provider`, `resource_type`, `resource_name`, and detect nested types (e.g., `servers/databases` for SQL).

### 2. Handler Registry (strategy pattern)
Each handler implements:
- `get_config(client, resource_id)` → `dict` of capacity values (e.g., `{cpu_cores: 4, memory_gb: 16}`)
- `get_metric_definitions()` → list of `(metric_name, unit, display_name)` tuples
- `get_resource_location(client, resource_id)` → Azure region string
- `list_skus(client, location)` → list of available SKUs with capacity specs
- `map_sku_to_config(sku)` → same shape dict as `get_config()`

**Supported handlers:**
| Provider/Type | Handler |
|---|---|
| `microsoft.compute/virtualmachines` | VMHandler |
| `microsoft.web/serverfarms` | AppServicePlanHandler |
| `microsoft.web/sites` | WebAppHandler (delegates to plan) |
| `microsoft.containerservice/managedclusters` | AKSHandler |
| `microsoft.sql/servers/databases` | SQLDatabaseHandler |
| `microsoft.storage/storageaccounts` | StorageAccountHandler |
| fallback | GenericHandler (CPU metric only) |

### 3. Metric Collection
```python
MonitorManagementClient.metrics.list(
    resource_uri=resource_id,
    timespan=f"{(now-7d).isoformat()}/{now.isoformat()}",
    interval="PT1H",
    metricnames=",".join(metric_names),
    aggregation="Average,Maximum,Minimum"
)
```
Return: `{metric_name: {avg, max, min, timeseries}}`

### 4. Baseline Calculation
Per metric:
```
baseline = min(1.3 * avg_metric, resource_capacity_for_metric)
```
Where `resource_capacity_for_metric` is the configured max (e.g., 100% for CPU, total_memory_gb for memory).

### 5. Rule Engine (priority order)

**Constants:**
```python
UNDER_THRESHOLD = 0.20
OVER_THRESHOLD  = 0.80
ARTIFICIAL_RATIO = 3.0
```

**Rules (checked in order):**
1. `resource_capacity > ARTIFICIAL_RATIO * peak_metric`  
   → **ARTIFICIAL_BASELINE**: config is >3x peak observed usage; baseline is inflated by config, not real load  
   → Recommend significantly smaller SKU

2. `avg_metric > OVER_THRESHOLD * baseline`  
   → **OVERUTILIZED**: resource is sustaining near-cap usage  
   → Recommend next tier up

3. `avg_metric < UNDER_THRESHOLD * baseline`  
   → **UNDERUTILIZED**: resource uses <20% of its baseline  
   → Recommend next tier down

4. Otherwise  
   → **NO_RECOMMENDATION**: SKU is appropriately sized

Decision uses the **worst-case metric** (the metric with highest relative utilization wins).

### 6. SKU Recommendation
From `list_skus()`, filter to SKUs in the same region, then:
- **UNDERUTILIZED**: find largest SKU still ≤ `1.3 * observed_peak` across all metrics
- **OVERUTILIZED**: find smallest SKU ≥ `1.5 * observed_avg` across all metrics  
- **ARTIFICIAL_BASELINE**: find smallest SKU ≥ `1.2 * observed_peak` (right-size to actual usage)
- **NO_RECOMMENDATION**: show current SKU, no change

### 7. Terminal Output (rich)
```
┌─ Azure SKU Recommendation ────────────────────────────┐
│ Resource : my-vm (Virtual Machine)                    │
│ Region   : eastus                                     │
│ Current  : Standard_D4s_v3 (4 vCPU, 16 GB RAM)       │
├─ 7-Day Metrics ───────────────────────────────────────┤
│ CPU Avg  : 12.3%   Peak: 34.1%   Baseline: 44.3%     │
│ Memory   : 3.1 GB  Peak: 5.2 GB  Baseline: 6.8 GB    │
├─ Verdict ─────────────────────────────────────────────┤
│ ⚠ UNDERUTILIZED (CPU avg 12.3% < 20% of baseline)    │
├─ Recommendation ──────────────────────────────────────┤
│ → Standard_B2ms  (2 vCPU, 8 GB RAM) — saves ~50%     │
└───────────────────────────────────────────────────────┘
```

### 8. Displaying cost difference between the original resource and recomended resource

---

## Verification
1. `uv add <deps>` then `python main.py`
2. Paste a real Azure VM resource URL
3. Confirm metrics fetch (check timeseries data printed in verbose mode)
4. Confirm baseline = min(1.3*avg, config) calculation shown
5. Confirm rule fires correctly for known over/under-utilized resource
6. Confirm SKU list fetched from correct region
