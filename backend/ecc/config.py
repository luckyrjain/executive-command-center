from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", validation_alias="ECC_ENV")
    database_url: str = Field(
        default="postgresql+psycopg://ecc:ecc@localhost:5432/ecc",
        validation_alias="ECC_DATABASE_URL",
    )
    session_secret: str = Field(
        default="",
        min_length=32,
        validation_alias="ECC_SESSION_SECRET",
    )
    cors_origins: str = Field(default="http://localhost:5173", validation_alias="ECC_CORS_ORIGINS")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
