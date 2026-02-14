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


settings = Settings()
