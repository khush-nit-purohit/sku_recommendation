from azure.identity import (
    AzureCliCredential,
    InteractiveBrowserCredential,
    EnvironmentCredential,
    ChainedTokenCredential,
    TokenCachePersistenceOptions,
)

def build_credential(cache_path: str) -> ChainedTokenCredential:
    """
    Order of precedence:
    1. Environment vars (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)
       — for CI/CD, service principals, managed identity
    2. Azure CLI token (az login) — for local developer use
    3. Interactive browser — fallback; token cached to disk via MSAL
    """
    cache_opts = TokenCachePersistenceOptions(
        name="resource_advisor",
        allow_unencrypted_storage=False,
    )
    return ChainedTokenCredential(
        EnvironmentCredential(),
        AzureCliCredential(),
        InteractiveBrowserCredential(
            cache_persistence_options=cache_opts,
        ),
    )
