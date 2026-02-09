from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.api import router
from src.database import init_db
from src.rate_limit import limiter, rate_limit_exceeded_handler

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create tables (for MVP)
    init_db()
    yield
    # Shutdown

app = FastAPI(
    title="Agent Trust Bureau",
    description="The Credit Bureau for AI Agents",
    version="0.2.0",
    lifespan=lifespan
)

# Serve static files
import os
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Add rate limiter to app state
app.state.limiter = limiter

# Add rate limit exception handler
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Include API routes (both at root and /v1 for compatibility)
app.include_router(router, prefix="/v1")
app.include_router(router) 

@app.get("/")
async def root():
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"service": "Agent Trust Bureau", "version": "0.2.0", "docs": "/docs"}
