# Resource Advisor — REST API Implementation Plan

## Overview

A cloud-agnostic resource recommendation engine exposed as a REST API. Given an Azure resource URL, it fetches 30 days of metrics, computes a configurable baseline, discovers available SKUs in the resource's region, and returns two ranked candidates — one cost-optimised and one performance-optimised — with actual cost deltas.

**Phase 1 scope:** Azure only. Auth via `az login` → browser fallback → cached credentials. Output is JSON throughout.

---

## Repository structure

```
resource-advisor/
├── pyproject.toml
├── README.md
├── PLAN.md                          ← this file
├── .env.example
├── Dockerfile
│
├── resource_advisor/
│   ├── __init__.py
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py              ← Pydantic BaseSettings, env + yaml
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── interfaces.py            ← CloudProvider ABC
│   │   ├── models.py                ← shared Pydantic models
│   │   └── engine.py                ← orchestration, provider-agnostic
│   │
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py                  ← CloudProvider abstract base
│   │   ├── registry.py              ← maps resource URL → provider
│   │   └── azure/
│   │       ├── __init__.py
│   │       ├── auth.py              ← ChainedTokenCredential
│   │       ├── resolver.py          ← parses ARM resource ID
│   │       ├── metrics.py           ← Azure Monitor adapter
│   │       ├── skus.py              ← Compute SKUs + resource-type SKU APIs
│   │       └── pricing.py           ← Azure Retail Prices API
│   │
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── baseline.py              ← 130% logic, configurable
│   │   ├── ranker.py                ← cost-first + perf candidate selection
│   │   └── cost.py                  ← cost delta calculation
│   │
│   └── api/
│       ├── __init__.py
│       ├── app.py                   ← FastAPI app factory
│       ├── dependencies.py          ← shared DI: engine, credentials
│       ├── middleware.py            ← request ID, timing, error normalisation
│       └── routes/
│           ├── __init__.py
│           ├── recommend.py         ← POST /v1/recommend
│           ├── health.py            ← GET /health, GET /ready
│           └── config.py            ← GET /v1/config (read current settings)
│
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── test_baseline.py
    │   ├── test_ranker.py
    │   └── test_resolver.py
    └── integration/
        ├── test_recommend_endpoint.py
        └── fixtures/
            ├── mock_metrics.json
            └── mock_skus.json
```

---

## Module-by-module specification

### 1. `config/settings.py`

Uses **Pydantic `BaseSettings`** — reads from environment variables first, falls back to `~/.resource_advisor/config.yaml`.

```python
class Settings(BaseSettings):
    # Baseline
    baseline_multiplier: float = 1.3        # configurable — the "130%"
    baseline_percentile: float = 0.99       # p99 of metric history
    lookback_days: int = 30
    min_from_current_config: bool = True    # baseline >= actual config

    # Azure
    azure_subscription_id: str | None = None
    azure_default_region: str = "eastus"

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 1
    request_timeout_seconds: int = 120

    # Credential cache
    token_cache_path: str = "~/.resource_advisor/token_cache.bin"

    class Config:
        env_prefix = "RA_"             # RA_BASELINE_MULTIPLIER=1.5 etc.
        env_file = ".env"
```

Every tuneable value in the system is exposed here. Callers can override per-request via the request body (see API schema below).

---

### 2. `core/models.py`

All data structures are **Pydantic v2 models**. These are the types that flow through the entire engine — provider-agnostic.

```python
class MetricSample(BaseModel):
    timestamp: datetime
    value: float

class MetricSeries(BaseModel):
    name: str          # "cpu_percent", "memory_gb", "iops_read", etc.
    unit: str
    samples: list[MetricSample]
    avg: float
    p95: float
    p99: float
    peak: float

class ResourceConfig(BaseModel):
    """The actual current provisioned spec — what you're paying for today."""
    sku_name: str
    cpu_cores: float
    memory_gb: float
    iops_max: int | None = None
    network_mbps: int | None = None
    monthly_cost_usd: float | None = None

class BaselineRequirement(BaseModel):
    """Minimum spec any recommended SKU must satisfy."""
    cpu_cores_min: float
    memory_gb_min: float
    iops_min: int | None = None
    network_mbps_min: int | None = None
    multiplier_used: float
    derived_from: str        # "peak_p99" | "current_config"

class SKUCandidate(BaseModel):
    sku_name: str
    cpu_cores: float
    memory_gb: float
    iops_max: int | None = None
    network_mbps: int | None = None
    monthly_cost_usd: float
    cost_delta_usd: float        # vs current; negative = cheaper
    cost_delta_pct: float
    clears_baseline: bool
    region: str
    advantage: str | None = None  # e.g. "AMD EPYC gen5, 20% better single-thread"

class RecommendationVerdict(str, Enum):
    UPSCALE = "UPSCALE"
    DOWNSCALE = "DOWNSCALE"
    NO_CHANGE = "NO_CHANGE"
    IMPROVE_BASELINE = "IMPROVE_BASELINE"
    # IMPROVE_BASELINE means: current SKU is fine but the baseline
    # config (130% floor) is set lower than your actual config,
    # suggesting your provisioned spec was never justified by usage.

class Recommendation(BaseModel):
    request_id: str
    resource_id: str
    resource_type: str
    region: str
    current_sku: str
    verdict: RecommendationVerdict
    confidence: Literal["high", "medium", "low"]
    baseline: BaselineRequirement
    cost_optimized: SKUCandidate
    performance: SKUCandidate
    metrics_summary: dict[str, MetricSeries]
    generated_at: datetime
    lookback_days: int
    provider: str              # "azure" | "gcp" | "aws"
```

---

### 3. `core/interfaces.py`

The abstract contract every cloud provider must implement. The engine never imports anything from `providers/` directly — it only calls these methods.

```python
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
```

Adding GCP or AWS later = implement this interface, register in `providers/registry.py`, done. No changes to the engine.

---

### 4. `providers/azure/auth.py`

Tries credentials in order, caches the result.

```python
from azure.identity import (
    AzureCliCredential,
    InteractiveBrowserCredential,
    EnvironmentCredential,
    ChainedTokenCredential,
    TokenCachePersistenceOptions,
)

def build_credential(cache_path: str) -> ChainedTokenCredential:
    """
    Order of precedence:
    1. Environment vars (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)
       — for CI/CD, service principals, managed identity
    2. Azure CLI token (az login) — for local developer use
    3. Interactive browser — fallback; token cached to disk via MSAL
    """
    cache_opts = TokenCachePersistenceOptions(
        name="resource_advisor",
        allow_unencrypted_storage=False,
    )
    return ChainedTokenCredential(
        EnvironmentCredential(),
        AzureCliCredential(),
        InteractiveBrowserCredential(
            cache_persistence_options=cache_opts,
        ),
    )
```

The credential object is created once at startup and injected via FastAPI's dependency injection into every request that needs it. It is **not** recreated per request.

---

### 5. `providers/azure/resolver.py`

Parses an ARM resource URL or ID into a normalised dict.

**Input formats it must handle:**
- Full ARM URL: `https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/{name}`
- ARM resource ID: `/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/{name}`
- Azure Portal URL: `https://portal.azure.com/#resource/subscriptions/{sub}/resourceGroups/{rg}/providers/...`
- Short form with subscription from settings: `Microsoft.Compute/virtualMachines/{rg}/{name}`

The resolver must:
1. Strip URL prefix to get the ARM ID path
2. Extract `subscription_id`, `resource_group`, `provider_namespace`, `resource_type`, `resource_name`
3. Call ARM to get the resource's location (region)
4. Return the normalised dict (see interface above)

Raise `ResourceNotFoundError` if ARM returns 404. Raise `UnsupportedResourceTypeError` if the resource type has no metrics adapter yet.

---

### 6. `providers/azure/metrics.py`

Fetches from **Azure Monitor Metrics API** (`/providers/microsoft.insights/metrics`).

**Metric mapping by resource type:**

| Resource type | Metrics fetched |
|---|---|
| `Microsoft.Compute/virtualMachines` | `Percentage CPU`, `Available Memory Bytes`, `Disk Read Bytes/sec`, `Disk Write Bytes/sec`, `Network In Total`, `Network Out Total` |
| `Microsoft.Web/sites` (App Service) | `CpuPercentage`, `MemoryWorkingSet`, `Requests`, `ResponseTime` |
| `Microsoft.Sql/servers/databases` | `cpu_percent`, `physical_data_read_percent`, `dtu_consumption_percent` |
| `Microsoft.Cache/Redis` | `percentProcessorTime`, `usedmemorypercentage`, `connectedclients` |
| `Microsoft.ServiceBus/namespaces` | `IncomingMessages`, `ActiveMessages`, `ThrottledRequests` |

Each metric is fetched with:
- `timespan`: last 30 days (from settings)
- `interval`: `PT1H` (hourly — fine enough for p99 computation)
- `aggregation`: `Average,Maximum`

From the hourly samples, compute `avg`, `p95`, `p99`, `peak` in Python (no Azure-side aggregation for percentiles — pull raw hourly values and compute locally).

---

### 7. `providers/azure/skus.py`

Two Azure APIs are needed depending on resource type:

**For compute (VMs):** `GET /subscriptions/{sub}/providers/Microsoft.Compute/skus?$filter=location eq '{region}'`
Returns every VM SKU with vCPUs, memory, max IOPS, max network bandwidth.

**For App Service:** `GET /subscriptions/{sub}/providers/Microsoft.Web/skus`

**For SQL:** `GET /subscriptions/{sub}/providers/Microsoft.Sql/locations/{region}/capabilities`

**For Redis:** `GET /subscriptions/{sub}/providers/Microsoft.Cache/skus`

After fetching, filter to:
- SKUs available in the resource's region and subscription (quota check)
- SKUs of the same family where possible (e.g. D-series → D-series variants first)
- Exclude preview / deprecated SKUs

---

### 8. `providers/azure/pricing.py`

Uses the **Azure Retail Prices API** — no authentication required, public endpoint.

```
GET https://prices.azure.com/api/retail/prices
  ?$filter=serviceName eq 'Virtual Machines'
    and armRegionName eq 'eastus'
    and skuName eq 'D4s v3'
    and priceType eq 'Consumption'
    and currencyCode eq 'USD'
```

Returns hourly retail price. Multiply by `730` to get monthly estimate.

Cache results in memory for the duration of the server process (prices change infrequently). Add a `cache_ttl: int = 3600` setting for how often to re-fetch.

---

### 9. `analysis/baseline.py`

```python
def compute_baseline(
    metrics: dict[str, MetricSeries],
    current_config: ResourceConfig,
    config: BaselineConfig,
) -> BaselineRequirement:
    """
    For each dimension (CPU, memory, IOPS, network):
      raw_requirement = metric.p99 * multiplier        (e.g. p99 * 1.3)
      if min_from_current_config:
        requirement = max(raw_requirement, current_config_value)
      else:
        requirement = raw_requirement
    
    derived_from = "peak_p99" if raw_requirement > current_config_value
                   else "current_config"
    """
```

The `derived_from` field in the output tells the user *why* the baseline is what it is — useful for diagnosing `IMPROVE_BASELINE` verdicts.

---

### 10. `analysis/ranker.py`

Takes the full list of available SKUs and returns exactly two candidates.

**Cost-optimised candidate:**
1. Filter to SKUs where all dimensions clear the baseline
2. Sort ascending by `monthly_cost_usd`
3. Return the cheapest one that clears all constraints
4. If none found: relax the `min_from_current_config` constraint and retry; if still none, return the current SKU with a `NO_CHANGE` verdict

**Performance candidate:**
1. Same filtered list as above (must still clear baseline)
2. Score each SKU: `score = (cpu_cores × 0.4) + (memory_gb/4 × 0.3) + (network_mbps/1000 × 0.2) + (iops/10000 × 0.1)` — weights are configurable
3. Exclude the cost-optimised pick if it was already selected
4. Return the highest-scoring SKU that is meaningfully different from the cost pick (at least 10% better on one dimension)
5. Populate `advantage` field with the primary differentiator

**Verdict logic:**
- Both candidates have lower cost than current → `DOWNSCALE`
- Both candidates have higher cost / higher spec than current → `UPSCALE`
- Cost candidate ≈ current and perf candidate is better → `NO_CHANGE` (current is already cost-optimal but a perf upgrade exists)
- Current SKU already clears baseline but baseline was floored by `current_config` (not by actual usage) → `IMPROVE_BASELINE`

---

### 11. `api/app.py`

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
from .routes import recommend, health, config
from .middleware import add_middleware
from ..config.settings import get_settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: build credential, warm pricing cache
    settings = get_settings()
    app.state.credential = build_credential(settings.token_cache_path)
    app.state.pricing_cache = {}
    yield
    # Shutdown: nothing to clean up

def create_app() -> FastAPI:
    app = FastAPI(
        title="Resource Advisor",
        version="0.1.0",
        description="Cloud resource right-sizing recommendations",
        lifespan=lifespan,
    )
    add_middleware(app)
    app.include_router(health.router)
    app.include_router(recommend.router, prefix="/v1")
    app.include_router(config.router, prefix="/v1")
    return app
```

---

### 12. `api/routes/recommend.py`

**Endpoint:** `POST /v1/recommend`

**Request body:**

```json
{
  "resource_url": "string",                    // required — ARM ID or full URL
  "subscription_id": "string",                 // optional — overrides settings
  "lookback_days": 30,                         // optional — overrides settings
  "baseline_multiplier": 1.3,                  // optional — overrides settings
  "baseline_percentile": 0.99,                 // optional
  "min_from_current_config": true              // optional
}
```

**Validation:**
- `resource_url` is required and must be a non-empty string
- `lookback_days` must be between 1 and 365
- `baseline_multiplier` must be between 1.0 and 3.0
- `baseline_percentile` must be between 0.5 and 1.0

**Response:** `200 OK` with full `Recommendation` JSON (see models above)

**Error responses:**

| Status | Error code | Condition |
|---|---|---|
| 400 | `INVALID_RESOURCE_URL` | Cannot parse resource URL |
| 400 | `UNSUPPORTED_RESOURCE_TYPE` | Resource type has no adapter yet |
| 401 | `AUTH_FAILED` | Could not acquire Azure credential |
| 404 | `RESOURCE_NOT_FOUND` | ARM returned 404 for the resource |
| 429 | `THROTTLED` | Azure Monitor rate limit hit; retry-after header set |
| 500 | `ENGINE_ERROR` | Unexpected error; includes trace ID |
| 504 | `TIMEOUT` | Metrics or SKU fetch exceeded `request_timeout_seconds` |

All errors follow a consistent envelope:
```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "No resource found at /subscriptions/.../virtualMachines/my-vm",
    "request_id": "uuid",
    "docs": "https://github.com/your-org/resource-advisor/wiki/errors#RESOURCE_NOT_FOUND"
  }
}
```

**Implementation steps inside the handler:**

```
1. Validate request body (Pydantic does this automatically)
2. Generate request_id (UUID4)
3. Resolve provider from registry (registry.get_provider(resource_url))
4. provider.resolve_resource(resource_url) → normalised resource dict
5. In parallel (asyncio.gather):
     a. provider.get_metrics(resource, lookback_days, percentile)
     b. provider.get_current_config(resource)
6. baseline = compute_baseline(metrics, current_config, baseline_config)
7. provider.list_available_skus(resource, subscription_id)
8. Attach pricing to each SKU (pricing.enrich_skus)
9. (cost_pick, perf_pick) = ranker.select_candidates(skus, baseline, current_config)
10. verdict = ranker.determine_verdict(current_config, cost_pick, baseline)
11. Return Recommendation
```

Steps 5a and 5b are parallelised with `asyncio.gather` — metrics and current config are independent fetches.

---

### 13. `api/middleware.py`

Three middleware layers, applied in order:

**Request ID middleware** — generates a UUID per request, attaches it to `request.state.request_id` and adds it to every response as `X-Request-ID` header.

**Timing middleware** — records `start_time` on request, adds `X-Response-Time-Ms` to response.

**Error normalisation middleware** — catches any unhandled exception and wraps it in the standard error envelope above. Logs the full traceback server-side, returns sanitised message to caller.

---

### 14. `api/routes/health.py`

**`GET /health`** — liveness probe. Returns `{"status": "ok"}` immediately. Used by load balancers and container orchestrators to know if the process is alive.

**`GET /ready`** — readiness probe. Checks:
1. Azure credential can be acquired (calls `.get_token("https://management.azure.com/.default")`)
2. Azure Retail Prices API is reachable (HEAD request)

Returns `{"status": "ready", "checks": {...}}` or `503` if any check fails. Used by Kubernetes to gate traffic until the server is actually usable.

---

### 15. `api/routes/config.py`

**`GET /v1/config`** — returns the currently active settings (redacts sensitive values like token cache paths). Useful for callers to confirm what defaults will be applied if they don't override in the request body.

```json
{
  "baseline_multiplier": 1.3,
  "baseline_percentile": 0.99,
  "lookback_days": 30,
  "min_from_current_config": true,
  "supported_providers": ["azure"],
  "supported_resource_types": [
    "Microsoft.Compute/virtualMachines",
    "Microsoft.Web/sites",
    "Microsoft.Sql/servers/databases",
    "Microsoft.Cache/Redis",
    "Microsoft.ServiceBus/namespaces"
  ]
}
```

---

## Dependencies

```toml
# pyproject.toml (runtime)
[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.111"
uvicorn = {extras = ["standard"], version = "^0.29"}
pydantic = "^2.7"
pydantic-settings = "^2.2"
azure-identity = "^1.16"
azure-mgmt-compute = "^31.0"
azure-mgmt-monitor = "^6.0"
azure-mgmt-resource = "^23.0"
azure-mgmt-web = "^7.2"
azure-mgmt-sql = "^3.0"
azure-mgmt-redis = "^14.0"
httpx = "^0.27"       # for Retail Prices API (async)
pyyaml = "^6.0"

[tool.poetry.dev-dependencies]
pytest = "^8.0"
pytest-asyncio = "^0.23"
pytest-httpx = "^0.30"
respx = "^0.21"       # mock httpx calls
ruff = "^0.4"
mypy = "^1.10"
```

---

## Running the server

```bash
# Install
pip install -e ".[dev]"

# Authenticate (first time)
az login

# Start server
uvicorn resource_advisor.api.app:create_app --factory --reload --port 8000

# Or via Docker
docker build -t resource-advisor .
docker run -p 8000:8000 \
  -e RA_AZURE_SUBSCRIPTION_ID=your-sub-id \
  -e AZURE_CLIENT_ID=... \
  -e AZURE_CLIENT_SECRET=... \
  -e AZURE_TENANT_ID=... \
  resource-advisor
```

---

## Example API call

```bash
curl -X POST http://localhost:8000/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "resource_url": "/subscriptions/abc123/resourceGroups/my-rg/providers/Microsoft.Compute/virtualMachines/my-vm",
    "lookback_days": 30,
    "baseline_multiplier": 1.3
  }'
```

Response:
```json
{
  "request_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "resource_id": "/subscriptions/abc123/resourceGroups/my-rg/providers/Microsoft.Compute/virtualMachines/my-vm",
  "resource_type": "Microsoft.Compute/virtualMachines",
  "region": "eastus",
  "current_sku": "Standard_D4s_v3",
  "verdict": "DOWNSCALE",
  "confidence": "high",
  "baseline": {
    "cpu_cores_min": 2.6,
    "memory_gb_min": 8.45,
    "multiplier_used": 1.3,
    "derived_from": "peak_p99"
  },
  "cost_optimized": {
    "sku_name": "Standard_D2s_v3",
    "cpu_cores": 2,
    "memory_gb": 8,
    "monthly_cost_usd": 70.08,
    "cost_delta_usd": -70.08,
    "cost_delta_pct": -50.0,
    "clears_baseline": true,
    "region": "eastus"
  },
  "performance": {
    "sku_name": "Standard_D4as_v5",
    "cpu_cores": 4,
    "memory_gb": 16,
    "monthly_cost_usd": 124.10,
    "cost_delta_usd": 14.02,
    "cost_delta_pct": 12.7,
    "clears_baseline": true,
    "region": "eastus",
    "advantage": "AMD EPYC gen5 — ~20% better single-thread vs D4s_v3 at similar cost"
  },
  "metrics_summary": {
    "cpu_percent": {
      "name": "cpu_percent",
      "avg": 12.4,
      "p95": 30.2,
      "p99": 38.1,
      "peak": 67.0
    },
    "memory_gb": {
      "name": "memory_gb",
      "avg": 5.2,
      "p95": 6.1,
      "p99": 6.5,
      "peak": 7.9
    }
  },
  "generated_at": "2026-05-07T14:32:00Z",
  "lookback_days": 30,
  "provider": "azure"
}
```

---

## Build order (recommended implementation sequence)

Build in this sequence to always have a runnable, testable system at each step:

1. **`core/models.py`** — all data types. No dependencies. Write unit tests immediately.
2. **`config/settings.py`** — settings with defaults. No Azure dependency.
3. **`core/interfaces.py`** — abstract base. No implementation yet.
4. **`api/app.py` + `api/routes/health.py`** — server skeleton that returns `{"status": "ok"}`. Runnable now.
5. **`api/middleware.py`** — request ID and error normalisation.
6. **`providers/azure/auth.py`** — credential chain. Test with `az login`.
7. **`providers/azure/resolver.py`** — URL parsing. Write unit tests with fixture ARM IDs.
8. **`providers/azure/metrics.py`** — Azure Monitor adapter. Write integration tests with mock HTTP responses.
9. **`providers/azure/skus.py`** — SKU discovery. Unit-testable with fixture JSON.
10. **`providers/azure/pricing.py`** — Retail Prices API. Mock the HTTP call in tests.
11. **`analysis/baseline.py`** — pure function, easy to unit test.
12. **`analysis/ranker.py`** — pure function, easy to unit test with fixture SKU lists.
13. **`core/engine.py`** — wires everything together.
14. **`api/routes/recommend.py`** — the main endpoint. Integration test end-to-end.
15. **`api/routes/config.py`** — trivial read of settings.
16. **`providers/registry.py`** — provider lookup by URL. Only needed once you add a second provider.

---

## Testing strategy

**Unit tests** (no Azure calls, no network):
- `test_baseline.py` — given fixture metrics + config, assert correct baseline values
- `test_ranker.py` — given fixture SKU list + baseline, assert correct cost/perf picks and verdict
- `test_resolver.py` — given fixture ARM IDs in various formats, assert correct parsing

**Integration tests** (mocked HTTP, no real Azure):
- Use `respx` to intercept all `httpx` calls and return fixture JSON
- `test_recommend_endpoint.py` — full POST → response cycle with mocked Azure APIs
- Assert response schema, correct verdict for known fixture data

**Manual smoke test:**
- Run server locally with real `az login`
- POST a real VM resource ID
- Verify response looks correct

---

## Adding a new cloud provider later

1. Create `providers/gcp/` with `auth.py`, `resolver.py`, `metrics.py`, `skus.py`, `pricing.py`
2. Implement `CloudProvider` interface in each module
3. Register in `providers/registry.py`:
   ```python
   PROVIDERS = [AzureProvider(), GcpProvider()]
   def get_provider(resource_url: str) -> CloudProvider:
       for p in PROVIDERS:
           if p.supports(resource_url):
               return p
       raise UnsupportedProviderError(resource_url)
   ```
4. Add new metric mappings for GCP resource types
5. No changes to `engine.py`, `ranker.py`, `baseline.py`, or any API routes

---

## Known limitations and future work

- **Quota awareness:** SKU availability per subscription is checked but quota limits are not. A SKU might be available in the region but your subscription may have zero quota for it. Add a quota check step in `skus.py` (`GET /subscriptions/{sub}/providers/Microsoft.Compute/locations/{region}/usages`).
- **Multi-metric constraints:** The ranker currently requires a SKU to clear *all* metric baselines simultaneously. For some resources (e.g. SQL DTU) the metrics are composite and the mapping to SKU dimensions is not 1:1. This needs resource-type-specific constraint logic in a future iteration.
- **Reserved instance pricing:** The Retail Prices API returns pay-as-you-go rates. If the customer uses 1-year or 3-year reservations the actual cost delta will differ significantly. Add a `pricing_model: "payg" | "1yr" | "3yr"` field to the request.
- **Spot / preemptible SKUs:** Excluded from candidates by default. Add an `include_spot: bool = false` setting.
- **Persistence:** Recommendations are not stored. Add an optional `results_storage_account` setting to write each recommendation to Azure Blob Storage as a JSON file for audit/history.
