from pydantic_settings import BaseSettings

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

    # SKU cache DB
    sku_db_path: str = "~/.resource_advisor/sku_cache.db"
    sku_cache_ttl_days: int = 7

    class Config:
        env_prefix = "RA_"             # RA_BASELINE_MULTIPLIER=1.5 etc.
        env_file = ".env"

def get_settings() -> Settings:
    return Settings()
