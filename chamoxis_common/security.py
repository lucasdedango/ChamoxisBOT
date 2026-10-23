"""Common protocol authentication, with distinct administrative credentials."""
import secrets
from fastapi import Header, HTTPException


def require_key(expected: str):
    if not expected:
        raise RuntimeError("An API key must be configured; unauthenticated APIs are forbidden")

    async def check(x_api_key: str = Header(default="")):
        if not secrets.compare_digest(expected, x_api_key):
            raise HTTPException(401, "Invalid API key")
    return check
