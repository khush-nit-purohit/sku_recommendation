from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel

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
    min_val: float = 0.0

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
