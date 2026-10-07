"""Runtime settings. Every value can be overridden with a GEO_-prefixed
environment variable (e.g. GEO_DATABASE_URL) or a .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/geomeasure.db"
    max_upload_bytes: int = 50 * 1024 * 1024         # largest accepted upload
    max_uncompressed_bytes: int = 500 * 1024 * 1024  # zip-bomb guard
    max_features: int = 100_000


settings = Settings()
