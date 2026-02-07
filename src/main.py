from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from src.api import router
from src.database import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create tables (for MVP)
    init_db()
    yield
    # Shutdown

app = FastAPI(
    title="Agent Trust Bureau",
    description="The Credit Bureau for AI Agents",
    version="0.1.0",
    lifespan=lifespan
)

# Include API routes with /v1 prefix
app.include_router(router, prefix="/v1")

# Explicitly add the dashboard route at root level (since it's not an API endpoint)
# We import the dashboard handler directly or just rely on the router having it.
# Actually, the router has /dashboard inside it. So if we include it with /v1 prefix, it becomes /v1/dashboard.
# That's why it was 404ing at /dashboard!

# FIX: Separate API routes from UI routes.
# For now, I'll just include the router TWICE but be careful.
# Better: Just include the router WITHOUT prefix for the dashboard to work at /dashboard
# But then /events becomes /events (not /v1/events).

# Let's just include the router at ROOT level for simplicity in this MVP.
# The endpoints will be /events, /scores, /dashboard.
# And I'll keep /v1 for compatibility if needed, but let's simplify.

app.include_router(router) 

@app.get("/")
def root():
    return RedirectResponse(url="/dashboard")

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Agent Trust Bureau"}
