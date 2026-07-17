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


class ConfigurationError(RuntimeError):
    """Raised when settings are unsafe for the declared deployment environment.

    Intentionally a plain ``RuntimeError`` subclass (not an HTTP exception) --
    this fires at process startup, before the ASGI app exists, so it must be
    safe to let bubble straight out of module import / ``uvicorn`` boot.
    """


_DEVELOPMENT_ENVIRONMENT = "development"

# Environments this deployment recognizes. Anything outside this set --
# including an empty string -- is rejected by validate_production_settings
# regardless of what it says, on a fail-closed basis: we cannot tell whether
# an unknown/blank ECC_ENV value is meant to be permissive or not, and a
# blank/typo'd environment is exactly the kind of mistake that could ship an
# insecure deployment silently. Only the exact recognized "development" value
# is treated as intentionally permissive; "staging" gets full production-grade
# validation alongside "production" since staging commonly carries
# production-like data and traffic.
_RECOGNIZED_ENVIRONMENTS = frozenset({"development", "staging", "production"})

# Substrings that flag a session secret as a known/likely development
# placeholder rather than a real generated secret. These are deliberately
# drawn from this repo's own defaults (.env.example, docker-compose.yml) plus
# generic markers, since `min_length=32` alone does not catch a
# long-but-still-placeholder value such as
# "development-only-secret-change-before-real-data" (49 chars).
_PLACEHOLDER_SECRET_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "please-change",
    "replace-with",
    "replace_with",
    "development-only",
    "placeholder",
    "example",
    "insecure",
    "sample-secret",
    "test-secret",
    "your-secret",
    "secret-change",
)

_MIN_PRODUCTION_SECRET_LENGTH = 32


def validate_production_settings(settings: Settings) -> None:
    """Fail fast when ``settings`` would be unsafe outside local development.

    Environment classification is always checked, even for a value that
    would otherwise be treated as permissive -- see
    ``_RECOGNIZED_ENVIRONMENTS`` above. Once the environment is known to be
    the exact "development" marker, this function returns immediately and
    today's permissive development defaults (empty session secret, HTTP
    localhost CORS origin) are left untouched. "staging" and "production"
    both receive the full tightened checks below.

    Note on scope: insecure production cookies and development-bootstrap
    reachability are deliberately NOT checked here. The only cookie-issuing
    code in the app is backend/ecc/dev_bootstrap.py, and the concrete fix for
    both of those categories is that ecc.main does not register the
    dev-bootstrap router at all outside development (see main.py and the
    router-registration tests in tests/test_production_security.py) --
    there is no separate Settings field for "cookies are secure" to check
    here without inventing an unused one, per the task brief's guidance.
    """
    environment = settings.environment.strip().casefold()
    if environment not in _RECOGNIZED_ENVIRONMENTS:
        raise ConfigurationError(
            "ECC_ENV must be one of "
            f"{sorted(_RECOGNIZED_ENVIRONMENTS)}; got {settings.environment!r}."
        )

    if environment == _DEVELOPMENT_ENVIRONMENT:
        return

    _validate_session_secret(settings.session_secret)
    _validate_cors_origins(settings.cors_origin_list)


def _validate_session_secret(secret: str) -> None:
    if len(secret) < _MIN_PRODUCTION_SECRET_LENGTH:
        raise ConfigurationError(
            "ECC_SESSION_SECRET must be at least "
            f"{_MIN_PRODUCTION_SECRET_LENGTH} characters outside development."
        )
    lowered = secret.casefold()
    for marker in _PLACEHOLDER_SECRET_MARKERS:
        if marker in lowered:
            raise ConfigurationError(
                "ECC_SESSION_SECRET looks like a development placeholder "
                f"(matched {marker!r}); set a unique random secret before "
                "deploying outside development."
            )


def _validate_cors_origins(origins: list[str]) -> None:
    if not origins:
        raise ConfigurationError("ECC_CORS_ORIGINS must not be empty outside development.")
    for origin in origins:
        if origin == "*":
            raise ConfigurationError(
                "ECC_CORS_ORIGINS must not contain a wildcard origin outside development."
            )
        if not origin.startswith("https://"):
            raise ConfigurationError(
                f"ECC_CORS_ORIGINS entry {origin!r} must use https:// outside development."
            )
