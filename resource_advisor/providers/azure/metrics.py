import datetime
from azure.mgmt.monitor.aio import MonitorManagementClient
from resource_advisor.core.models import MetricSeries, MetricSample

# Metric mapping by resource type
METRICS_MAP = {
    "microsoft.compute/virtualmachines": ["Percentage CPU", "Available Memory Bytes"], # simplified for now
    "microsoft.web/sites": ["CpuPercentage", "MemoryWorkingSet"],
    "microsoft.sql/servers/databases": ["cpu_percent", "dtu_consumption_percent"],
}

async def get_metrics(resource: dict, lookback_days: int, credential, percentile: float = 0.99) -> dict[str, MetricSeries]:
    """
    Fetch metric history for the resource.
    """
    resource_type = resource["resource_type"].lower()
    resource_id = resource["resource_id"]
    
    metric_names = METRICS_MAP.get(resource_type, [])
    if not metric_names:
        return {}

    client = MonitorManagementClient(credential, resource["subscription_id"])
    
    end_time = datetime.datetime.now(datetime.timezone.utc)
    start_time = end_time - datetime.timedelta(days=lookback_days)
    timespan = f"{start_time.isoformat()}/{end_time.isoformat()}"
    
    results = {}
    
    # In a full implementation, we'd query all metrics at once if possible,
    # or loop through them
    response = await client.metrics.list(
        resource_uri=resource_id,
        timespan=timespan,
        interval="PT1H",
        metricnames=",".join(metric_names),
        aggregation="Average,Maximum"
    )
    
    # Process response
    if hasattr(response, 'value'):
        for metric in response.value:
            name = metric.name.value
            unit = metric.unit.name
            samples = []
            
            # Extract samples
            for timeseries in metric.timeseries:
                for data in timeseries.data:
                    # Using average for general usage, maximum for peak
                    val = data.average if data.average is not None else (data.maximum if data.maximum is not None else 0.0)
                    if data.time_stamp:
                        samples.append(MetricSample(timestamp=data.time_stamp, value=val))
                    
            if not samples:
                continue
                
            values = [s.value for s in samples]
            values.sort()

            avg = sum(values) / len(values)
            peak = max(values)
            min_val = min(values)

            def get_percentile(data, p):
                idx = min(round((len(data) - 1) * p), len(data) - 1)
                return data[idx]

            p95 = get_percentile(values, 0.95)
            p99 = get_percentile(values, percentile)

            results[name] = MetricSeries(
                name=name,
                unit=unit,
                samples=samples,
                avg=avg,
                p95=p95,
                p99=p99,
                peak=peak,
                min_val=min_val,
            )
            
    return results
