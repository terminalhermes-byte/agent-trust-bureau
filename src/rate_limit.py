"""
Rate Limiting for Agent Trust Bureau

Uses slowapi for simple in-memory rate limiting.
In production, configure Redis backend for distributed limiting.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from fastapi.responses import JSONResponse


def get_api_key_or_ip(request: Request) -> str:
    """
    Rate limit key: use API key if provided, otherwise IP address.
    This allows higher limits for authenticated requests.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    return f"ip:{get_remote_address(request)}"


# Create limiter instance
limiter = Limiter(key_func=get_api_key_or_ip)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Custom handler for rate limit exceeded."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": f"Rate limit exceeded: {exc.detail}",
            "retry_after": getattr(exc, 'retry_after', 60)
        }
    )


# Rate limit constants
AUTHENTICATED_LIMIT = "100/minute"
PUBLIC_LIMIT = "30/minute"
