from resource_advisor.core.models import SKUCandidate, BaselineRequirement, ResourceConfig, RecommendationVerdict

def select_candidates(
    skus: list[SKUCandidate], 
    baseline: BaselineRequirement, 
    current_config: ResourceConfig,
    relax_constraints: bool = False
) -> tuple[SKUCandidate | None, SKUCandidate | None]:
    
    valid_skus = []
    for sku in skus:
        if sku.cpu_cores >= baseline.cpu_cores_min and sku.memory_gb >= baseline.memory_gb_min:
            sku.clears_baseline = True
            valid_skus.append(sku)
            
    if not valid_skus and not relax_constraints:
        # Try relaxing if none found
        relaxed_baseline = BaselineRequirement(
            cpu_cores_min=baseline.cpu_cores_min * 0.8,
            memory_gb_min=baseline.memory_gb_min * 0.8,
            iops_min=baseline.iops_min,
            network_mbps_min=baseline.network_mbps_min,
            multiplier_used=baseline.multiplier_used,
            derived_from=baseline.derived_from
        )
        return select_candidates(skus, relaxed_baseline, current_config, relax_constraints=True)
        
    if not valid_skus:
        return None, None

    # Cost-optimised: sort by cost asc
    valid_skus.sort(key=lambda x: x.monthly_cost_usd)
    cost_pick = valid_skus[0]

    # Calculate delta against current config
    current_cost = current_config.monthly_cost_usd or (cost_pick.monthly_cost_usd * 2) # fallback
    
    for sku in valid_skus:
        sku.cost_delta_usd = sku.monthly_cost_usd - current_cost
        sku.cost_delta_pct = (sku.cost_delta_usd / current_cost) * 100.0 if current_cost > 0 else 0.0

    # Performance pick: score based on resources
    perf_pick = None
    best_score = -1
    
    for sku in valid_skus:
        if sku.sku_name == cost_pick.sku_name:
            continue
            
        score = (sku.cpu_cores * 0.4) + (sku.memory_gb / 4 * 0.3)
        if score > best_score:
            best_score = score
            perf_pick = sku
            
    if perf_pick:
        if perf_pick.cpu_cores > cost_pick.cpu_cores:
            perf_pick.advantage = "Higher CPU capacity"
        elif perf_pick.memory_gb > cost_pick.memory_gb:
            perf_pick.advantage = "Higher Memory capacity"
        else:
            perf_pick.advantage = "Better overall performance score"

    return cost_pick, perf_pick

def determine_verdict(current_config: ResourceConfig, cost_pick: SKUCandidate | None, baseline: BaselineRequirement) -> RecommendationVerdict:
    if not cost_pick:
        return RecommendationVerdict.NO_CHANGE
        
    current_cost = current_config.monthly_cost_usd or 0.0
    
    if cost_pick.monthly_cost_usd < current_cost:
        return RecommendationVerdict.DOWNSCALE
    elif cost_pick.monthly_cost_usd > current_cost:
        return RecommendationVerdict.UPSCALE
    elif baseline.derived_from == "current_config":
        return RecommendationVerdict.IMPROVE_BASELINE
    else:
        return RecommendationVerdict.NO_CHANGE
