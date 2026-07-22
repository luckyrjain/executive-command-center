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
    # No `min_length` constraint here (deliberately, see below) -- the
    # >=32-character requirement is enforced by validate_production_settings's
    # _validate_session_secret, but only outside development. pydantic-settings
    # validates a field's constraints against its *resolved* value regardless
    # of whether that value came from a source or the field's own default
    # (unlike plain pydantic BaseModel, it does not skip validation for
    # unset/default fields) -- so a `min_length=32` constraint here would
    # make Settings() itself raise before validate_production_settings's
    # ECC_ENV=development early-return is ever reached, breaking the
    # documented "permissive empty session secret in development" contract
    # in every environment, not just production. This was live and confirmed
    # in CI: the `containers` job's smoke test, which boots the real
    # container with ECC_ENV=development, failed with exactly this
    # ValidationError until this constraint was removed.
    session_secret: str = Field(default="", validation_alias="ECC_SESSION_SECRET")
    cors_origins: str = Field(default="http://localhost:5173", validation_alias="ECC_CORS_ORIGINS")
    metrics_token: str = Field(default="", validation_alias="ECC_METRICS_TOKEN")
    # Number of trusted reverse proxies/load balancers in front of this app
    # (e.g. 1 for a single nginx/ALB hop). 0 (the default) means "not behind
    # a proxy" -- ecc.http_security trusts only the raw ASGI socket peer,
    # which is correct for local/dev/test but resolves to the proxy's own
    # address in any real deployment, collapsing every distinct client into
    # one shared mutation-rate-limit bucket. Set to the *exact* hop count
    # from docs/runbooks/PHASE-1-DEPLOYMENT.md's topology -- too low leaves
    # the collapsed-bucket problem partially unfixed, but too high is the
    # dangerous direction: it lets a client pad X-Forwarded-For with its own
    # fabricated leading hops until the header is long enough that the
    # trusted-hop count selects one of those attacker-controlled values
    # instead of the real proxy-appended one, letting it mint a fresh fake
    # client IP per request and fully bypass the per-IP rate limit. See
    # ecc.http_security._client_ip_from_forwarded_for for why this must be an
    # exact trusted count, not "trust X-Forwarded-For whenever present" --
    # the header's client-supplied left-hand portion is never trustworthy.
    trusted_proxy_count: int = Field(default=0, ge=0, validation_alias="ECC_TRUSTED_PROXY_COUNT")
    # Off by default: loading the local sentence-transformers model costs a
    # multi-second first-call delay and, before it's been cached to disk once,
    # a Hugging Face Hub download -- unacceptable in the default/test
    # environment where nothing needs semantic search. Mutation paths
    # (queue_embedding) and hybrid retrieval both treat this as "feature not
    # provisioned here" and degrade to lexical-only rather than erroring, per
    # RETRIEVAL-CONTRACT.md's degradation rule -- this flag is the deliberate,
    # explicit way to opt into paying that cost, not a workaround for it.
    embeddings_enabled: bool = Field(default=False, validation_alias="ECC_EMBEDDINGS_ENABLED")

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
# generic markers, since the length check alone does not catch a
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
