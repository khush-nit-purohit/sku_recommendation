from contextlib import asynccontextmanager
from fastapi import FastAPI
from .routes import health, recommend, config
from .middleware import add_middleware
from resource_advisor.config.settings import get_settings
from resource_advisor.providers.azure.auth import build_credential
from resource_advisor.providers.registry import registry
from resource_advisor.providers.azure.provider import AzureProvider

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: build credential, warm pricing cache
    settings = get_settings()
    app.state.credential = build_credential(settings.token_cache_path)
    app.state.pricing_cache = {}
    
    # Register providers
    azure_provider = AzureProvider(app.state.credential, settings.azure_subscription_id)
    registry.register(azure_provider)
    
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
