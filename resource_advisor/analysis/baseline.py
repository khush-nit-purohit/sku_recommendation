from resource_advisor.core.models import MetricSeries, ResourceConfig, BaselineRequirement
from resource_advisor.config.settings import Settings

def compute_baseline(
    metrics: dict[str, MetricSeries],
    current_config: ResourceConfig,
    config: Settings,
) -> BaselineRequirement:
    """
    Compute baseline requirements based on metrics and current config.
    """
    cpu_p99 = 0.0
    mem_p99 = 0.0
    
    if "Percentage CPU" in metrics or "cpu_percent" in metrics or "CpuPercentage" in metrics:
        # Assuming percentage 0-100
        metric_name = next((m for m in ["Percentage CPU", "cpu_percent", "CpuPercentage"] if m in metrics), None)
        if metric_name:
            cpu_p99 = (metrics[metric_name].p99 / 100.0) * current_config.cpu_cores
        
    if "Available Memory Bytes" in metrics:
        # peak used = total - min(available); min_val = lowest observed free memory
        mem_bytes = metrics["Available Memory Bytes"].min_val
        mem_gb = mem_bytes / (1024 ** 3)
        mem_p99 = max(0.0, current_config.memory_gb - mem_gb)
    elif "MemoryWorkingSet" in metrics:
        # App service memory used
        mem_bytes = metrics["MemoryWorkingSet"].p99
        mem_p99 = mem_bytes / (1024 ** 3)

    raw_cpu_req = cpu_p99 * config.baseline_multiplier
    raw_mem_req = mem_p99 * config.baseline_multiplier
    
    cpu_req = raw_cpu_req
    mem_req = raw_mem_req
    derived_from = "peak_p99"

    if config.min_from_current_config:
        if raw_cpu_req < current_config.cpu_cores or raw_mem_req < current_config.memory_gb:
            cpu_req = max(raw_cpu_req, current_config.cpu_cores)
            mem_req = max(raw_mem_req, current_config.memory_gb)
            derived_from = "current_config"
            
    # For now, default to current config if no metrics found
    if cpu_req == 0 and mem_req == 0:
        cpu_req = current_config.cpu_cores
        mem_req = current_config.memory_gb
        derived_from = "current_config"

    return BaselineRequirement(
        cpu_cores_min=cpu_req,
        memory_gb_min=mem_req,
        iops_min=None,
        network_mbps_min=None,
        multiplier_used=config.baseline_multiplier,
        derived_from=derived_from
    )
