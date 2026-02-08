"""
API Key Authentication for Agent Trust Bureau

Configure allowed API keys via environment variable or config file.
"""
import os
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

# API Key configuration
# In production, load from environment or secrets manager
VALID_API_KEYS = set(
    key.strip() 
    for key in os.getenv("ATB_API_KEYS", "test-key-1,test-key-2").split(",")
    if key.strip()
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key: str = Security(api_key_header)) -> str:
    """
    Dependency that requires a valid API key.
    Use for protected endpoints like /events.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide X-API-Key header."
        )
    if api_key not in VALID_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key."
        )
    return api_key


async def optional_api_key(api_key: str = Security(api_key_header)) -> str | None:
    """
    Dependency that optionally validates API key.
    Returns None if no key provided, raises if invalid key provided.
    """
    if not api_key:
        return None
    if api_key not in VALID_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key."
        )
    return api_key
