from fastapi import APIRouter, Depends
from resource_advisor.config.settings import get_settings, Settings

router = APIRouter()

@router.get("/config")
async def get_current_config(settings: Settings = Depends(get_settings)):
    # Redact sensitive info
    config_dict = settings.model_dump()
    if "token_cache_path" in config_dict:
        config_dict["token_cache_path"] = "***"
        
    return config_dict
