from typing import Type
from resource_advisor.core.interfaces import CloudProvider

class UnsupportedProviderError(Exception):
    def __init__(self, resource_url: str):
        super().__init__(f"No registered cloud provider supports URL: {resource_url}")
        self.status_code = 400
        self.error_code = "UNSUPPORTED_PROVIDER"

class ProviderRegistry:
    def __init__(self):
        self._providers: list[CloudProvider] = []

    def register(self, provider: CloudProvider):
        self._providers.append(provider)

    def get_provider(self, resource_url: str) -> CloudProvider:
        for p in self._providers:
            if p.supports(resource_url):
                return p
        raise UnsupportedProviderError(resource_url)

registry = ProviderRegistry()
