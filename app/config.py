from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Agent Trust Bureau"
    environment: str = "development"
    api_prefix: str = "/v1"
    model_version: str = "v0.1-baseline"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/agent_trust_bureau"
    db_echo: bool = False
    auto_create_tables: bool = False
    require_auth: bool = False  # set True in production; when False, all /v1 routes use default tenant
    score_rate_limit_per_minute: int = 30
    webhook_async: bool = False  # set True in production to use DB-backed webhook queue + worker
    webhook_worker_poll_seconds: float = 2.0  # how often the worker polls for pending jobs

    @model_validator(mode="after")
    def _normalize_database_url(self) -> "Settings":
        """Rewrite postgres:// and postgresql:// to postgresql+psycopg://.

        Render and Fly provide ``postgres://`` connection strings, but
        SQLAlchemy 2.x with the psycopg (v3) driver requires the full
        ``postgresql+psycopg://`` scheme.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            self.database_url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://") and "+psycopg" not in url:
            self.database_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return self


settings = Settings()
