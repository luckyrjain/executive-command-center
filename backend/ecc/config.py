from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ECC_", env_file=".env", extra="ignore")

    environment: str = Field(default="development", alias="ENV")
    database_url: str = "postgresql+psycopg://ecc:ecc@localhost:5432/ecc"
    session_secret: str = Field(min_length=32)
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
