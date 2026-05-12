import re
from azure.mgmt.resource.aio import ResourceManagementClient

class ResourceResolverError(Exception):
    def __init__(self, message: str, status_code: int = 400, error_code: str = "INVALID_RESOURCE_URL"):
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(self.message)

class UnsupportedResourceTypeError(ResourceResolverError):
    def __init__(self, resource_type: str):
        super().__init__(
            f"Resource type '{resource_type}' is not currently supported.", 
            status_code=400, 
            error_code="UNSUPPORTED_RESOURCE_TYPE"
        )

def parse_resource_id(resource_url: str, default_subscription: str | None = None) -> dict:
    """
    Parse an ARM resource URL or ID into a normalised dict.
    Handles:
    - Full ARM URL
    - ARM resource ID
    - Azure Portal URL
    - Short form (requires default subscription)
    """
    # Strip URL prefixes
    if "management.azure.com" in resource_url:
        path = resource_url.split("management.azure.com", 1)[1]
    elif "portal.azure.com" in resource_url:
        if "#resource/" in resource_url:
            path = "/" + resource_url.split("#resource/", 1)[1]
        elif "#@m" in resource_url and "/resource/" in resource_url:
             path = "/" + resource_url.split("/resource/", 1)[1]
        else:
            path = resource_url
    else:
        path = resource_url

    path = path.split("?")[0] # remove query params
    if not path.startswith("/"):
        path = "/" + path
        
    # Match standard ARM ID pattern
    pattern = r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/([^/]+)/([^/]+(?:/[^/]+)*?)/([^/]+)$"
    match = re.match(pattern, path, re.IGNORECASE)
    
    if match:
        sub_id, rg, provider, resource_type, name = match.groups()
        full_type = f"{provider}/{resource_type}"
        return {
            "resource_id": path,
            "subscription_id": sub_id,
            "resource_group": rg,
            "resource_type": full_type,
            "name": name,
        }

    # Match short form if default sub is provided
    # Format: Provider.Namespace/type/rg/name
    short_pattern = r"^/([^/]+)/([^/]+(?:/[^/]+)*?)/([^/]+)/([^/]+)$"
    match = re.match(short_pattern, path, re.IGNORECASE)
    if match and default_subscription:
        provider, resource_type, rg, name = match.groups()
        full_type = f"{provider}/{resource_type}"
        arm_id = f"/subscriptions/{default_subscription}/resourceGroups/{rg}/providers/{full_type}/{name}"
        return {
            "resource_id": arm_id,
            "subscription_id": default_subscription,
            "resource_group": rg,
            "resource_type": full_type,
            "name": name,
        }

    raise ResourceResolverError(f"Cannot parse resource URL/ID: {resource_url}")
    
async def resolve_resource(resource_url: str, credential, default_subscription: str | None = None) -> dict:
    """
    Parse the URL and optionally look up the region using Azure Resource Graph or Resource client.
    For now, we'll return the parsed ID, region will be looked up later or passed in.
    """
    parsed = parse_resource_id(resource_url, default_subscription)

    client = ResourceManagementClient(credential, parsed["subscription_id"])
    try:
        resource = await client.resources.get_by_id(parsed["resource_id"], "2022-09-01")
        parsed["region"] = resource.location
    finally:
        await client.close()

    return parsed
