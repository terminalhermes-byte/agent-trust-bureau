from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import init_db
from app.routers import admin, events, policy, trust


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_tables:
        init_db()
    yield


APP_VERSION = "1.0.0"

app = FastAPI(title=settings.app_name, version=APP_VERSION, lifespan=lifespan)
app.include_router(events.router, prefix=settings.api_prefix)
app.include_router(trust.router, prefix=settings.api_prefix)
app.include_router(policy.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "version": APP_VERSION,
        "status": "ok",
        "docs": "/docs",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION, "environment": settings.environment}
