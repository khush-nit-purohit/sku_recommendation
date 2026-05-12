import json
import sqlite3
from typing import Dict, Optional

import httpx

# In-memory cache. Key format: {region}_{sku_name}_{service_name}
_pricing_cache: Dict[str, float] = {}

# Optional persistent DB connection — set via set_db_conn() from app startup or main.py
_db_conn: Optional[sqlite3.Connection] = None


def set_db_conn(conn: sqlite3.Connection):
    global _db_conn
    _db_conn = conn


async def get_retail_price(region: str, sku_name: str, service_name: str = "Virtual Machines") -> float | None:
    cache_key = f"{region}_{sku_name}_{service_name}"
    if cache_key in _pricing_cache:
        return _pricing_cache[cache_key]

    # Check persistent DB before hitting the network
    if _db_conn is not None:
        from resource_advisor.db.queries import get_price, upsert_price
        cached = get_price(_db_conn, region, sku_name, service_name)
        if cached is not None:
            _pricing_cache[cache_key] = cached
            return cached

    url = "https://prices.azure.com/api/retail/prices"

    clean_sku = sku_name.replace("Standard_", "").replace("_", " ")

    params = {
        "$filter": (
            f"serviceName eq '{service_name}' and armRegionName eq '{region}' "
            f"and skuName eq '{clean_sku}' and priceType eq 'Consumption' and currencyCode eq 'USD'"
        )
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            items = data.get("Items", [])

            if not items:
                params["$filter"] = (
                    f"serviceName eq '{service_name}' and armRegionName eq '{region}' "
                    f"and skuName eq '{sku_name}' and priceType eq 'Consumption' and currencyCode eq 'USD'"
                )
                response = await client.get(url, params=params, timeout=10.0)
                response.raise_for_status()
                items = response.json().get("Items", [])

            if items:
                price = items[0].get("retailPrice")
                if price is not None:
                    _pricing_cache[cache_key] = price
                    if _db_conn is not None:
                        from resource_advisor.db.queries import upsert_price
                        upsert_price(_db_conn, region, sku_name, service_name, price)
                    return price

        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError):
            pass

    return None


async def enrich_skus_with_pricing(skus: list, region: str, service_name: str = "Virtual Machines"):
    for sku in skus:
        hourly_price = await get_retail_price(region, sku.sku_name, service_name)
        if hourly_price is not None:
            sku.monthly_cost_usd = hourly_price * 730
        else:
            sku.monthly_cost_usd = 999999.0
