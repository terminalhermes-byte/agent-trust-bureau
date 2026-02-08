from fastapi import FastAPI
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

# Add rate limiter to app state
app.state.limiter = limiter

# Add rate limit exception handler
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Include API routes (both at root and /v1 for compatibility)
app.include_router(router, prefix="/v1")
app.include_router(router) 

@app.get("/")
def root():
    return RedirectResponse(url="/dashboard")

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Agent Trust Bureau", "version": "0.2.0"}
