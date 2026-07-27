"""Connector credential encryption at rest (design doc Decision 2:
`docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md`).

`connector_accounts.encrypted_credentials` never holds a plaintext OAuth
token or personal access token -- `encrypt_credential`/`decrypt_credential`
below are the only functions in this codebase that touch a connector
credential's plaintext form, and the decrypted value is held only for the
duration of the caller's own request/sync call, never logged, returned by
an API response, or written into an audit/outbox payload (`ecc.domains.
engineering.connector_accounts` is the one caller of `encrypt_credential`;
a later task's real GitHub/GitLab/Jira sync code is the one caller of
`decrypt_credential`).

Uses Fernet (`cryptography` package, RFC-005 v1.4.0 amendment) -- symmetric
authenticated encryption (AES-128-CBC + HMAC-SHA256), the standard choice
for "encrypt a secret at rest, decrypt it later with the same key" where no
asymmetric/multi-party requirement exists. `ECC_CONNECTOR_TOKEN_ENCRYPTION_
KEY` is a dedicated key, distinct from `session_secret` -- session_secret
signs/authenticates session and CSRF material; reusing it here would mean a
session-secret rotation also silently breaks every stored connector
credential's decryptability, an unrelated blast radius this module avoids
by keying the two independently.
"""

from base64 import urlsafe_b64encode
from functools import lru_cache
from hashlib import sha256

from cryptography.fernet import Fernet

from ecc.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    """Cached per-process -- `Settings` itself is already process-cached
    (`ecc.config.get_settings`), and re-deriving/re-validating the same key
    material on every encrypt/decrypt call would be pure overhead.

    Outside development, `ecc.config.validate_production_settings` (run at
    process startup, before this function's first real call) already
    rejects an empty or malformed `connector_token_encryption_key` --
    reaching the empty-key branch below with `environment != "development"`
    would mean startup validation was skipped entirely (a test importing
    this module directly without going through `ecc.main`), so the
    fallback below is explicitly gated on the development marker rather
    than "key happens to be empty," matching `session_secret`'s own
    permissive-in-development-only shape.
    """
    settings = get_settings()
    key = settings.connector_token_encryption_key
    if not key:
        if settings.environment.strip().casefold() != "development":
            raise RuntimeError(
                "ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY is unset outside development; "
                "ecc.config.validate_production_settings should have already rejected "
                "this at startup."
            )
        # Deterministic development-only fallback, derived from session_secret
        # (or a fixed placeholder if that too is unset) so local dev needs no
        # second secret configured. Never reachable in staging/production --
        # see the guard above.
        material = (settings.session_secret or "ecc-phase6-development-only-fallback").encode(
            "utf-8"
        )
        key = urlsafe_b64encode(sha256(material).digest()).decode("ascii")
    return Fernet(key.encode("ascii"))


def encrypt_credential(plaintext: str) -> bytes:
    """`plaintext` is a raw OAuth token / personal access token string.
    Returns Fernet's own URL-safe-base64 ciphertext token as bytes, the
    exact value `connector_accounts.encrypted_credentials` (`bytea`) stores.
    """
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_credential(ciphertext: bytes) -> str:
    """Inverse of `encrypt_credential`. Raises `cryptography.fernet.
    InvalidToken` if `ciphertext` was not produced by the currently
    configured key (e.g. `ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY` rotated
    without re-encrypting stored credentials) -- callers must treat that as
    a connector needing re-authorization, not retry the decrypt.
    """
    return _fernet().decrypt(ciphertext).decode("utf-8")
