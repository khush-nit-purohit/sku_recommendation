from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field

from resource_advisor.core.engine import generate_recommendation
from resource_advisor.config.settings import get_settings, Settings
from resource_advisor.providers.registry import UnsupportedProviderError
from resource_advisor.providers.azure.resolver import ResourceResolverError

router = APIRouter()

class RecommendRequest(BaseModel):
    resource_url: str = Field(..., min_length=1)
    subscription_id: str | None = None
    lookback_days: int | None = Field(None, ge=1, le=365)
    baseline_multiplier: float | None = Field(None, ge=1.0, le=3.0)
    baseline_percentile: float | None = Field(None, ge=0.5, le=1.0)
    min_from_current_config: bool | None = None

@router.post("/recommend")
async def recommend_endpoint(req: RecommendRequest, request: Request, settings: Settings = Depends(get_settings)):
    request_id = getattr(request.state, "request_id", None)

    # Build per-request overrides without mutating the shared settings instance
    override_dict = {k: v for k, v in req.model_dump().items() if v is not None and k != "resource_url"}
    if "subscription_id" in override_dict:
        override_dict["azure_subscription_id"] = override_dict.pop("subscription_id")

    effective_settings = settings.model_copy(update=override_dict)

    try:
        recommendation = await generate_recommendation(
            resource_url=req.resource_url,
            settings=effective_settings,
            request_id=request_id
        )
        return recommendation

    except UnsupportedProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ResourceResolverError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
