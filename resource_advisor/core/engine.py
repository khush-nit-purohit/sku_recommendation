import uuid
import datetime
import asyncio
from resource_advisor.core.models import Recommendation, RecommendationVerdict, SKUCandidate
from resource_advisor.config.settings import Settings
from resource_advisor.providers.registry import registry
from resource_advisor.analysis.baseline import compute_baseline
from resource_advisor.analysis.ranker import select_candidates, determine_verdict

async def generate_recommendation(
    resource_url: str,
    settings: Settings,
    request_id: str | None = None
) -> Recommendation:
    req_id = request_id or str(uuid.uuid4())
    
    # 1. Resolve Provider
    provider = registry.get_provider(resource_url)
    
    # 2. Resolve Resource
    resource = await provider.resolve_resource(resource_url)
    
    # 3. Fetch Metrics and Current Config concurrently
    metrics_task = provider.get_metrics(resource, settings.lookback_days, settings.baseline_percentile)
    config_task = provider.get_current_config(resource)
    
    metrics, current_config = await asyncio.gather(metrics_task, config_task)
    
    # 4. Compute Baseline
    baseline = compute_baseline(metrics, current_config, settings)
    
    # 5. List SKUs
    sub_id = settings.azure_subscription_id or resource.get("subscription_id")
    skus = await provider.list_available_skus(resource, sub_id)
    
    # 6. Rank Candidates
    cost_pick, perf_pick = select_candidates(skus, baseline, current_config)
    
    if not cost_pick:
        # Fallback if we couldn't find anything
        cost_pick = SKUCandidate(
            sku_name=current_config.sku_name,
            cpu_cores=current_config.cpu_cores,
            memory_gb=current_config.memory_gb,
            monthly_cost_usd=current_config.monthly_cost_usd or 0.0,
            cost_delta_usd=0.0,
            cost_delta_pct=0.0,
            clears_baseline=True,
            region=resource.get("region", "unknown")
        )
        perf_pick = cost_pick
        
    if not perf_pick:
        perf_pick = cost_pick

    # 7. Determine Verdict
    verdict = determine_verdict(current_config, cost_pick, baseline)
    
    return Recommendation(
        request_id=req_id,
        resource_id=resource["resource_id"],
        resource_type=resource["resource_type"],
        region=resource.get("region", "unknown"),
        current_sku=current_config.sku_name,
        verdict=verdict,
        confidence="medium", # Simplified
        baseline=baseline,
        cost_optimized=cost_pick,
        performance=perf_pick,
        metrics_summary=metrics,
        generated_at=datetime.datetime.now(datetime.timezone.utc),
        lookback_days=settings.lookback_days,
        provider="azure" if "azure" in provider.__class__.__name__.lower() else "unknown"
    )
