from fastapi import FastAPI
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

app.include_router(router, prefix="/v1")

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Agent Trust Bureau"}
