"""Personal-domain field-level encryption at rest (design doc Decision 3:
`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`).

Mirrors `ecc.domains.engineering.crypto`'s shape (Fernet, a dedicated key,
decrypted value never logged/returned by an unrelated response/written to
an audit or outbox payload), with one deliberate difference: this module's
`encrypt_field`/`decrypt_field` operate on `str`, not `bytes`. `domain_
records.payload` is JSONB, which cannot hold raw bytes -- Fernet's own
ciphertext token is already URL-safe-base64 ASCII text, so this module
simply keeps it as a `str` rather than round-tripping through bytes the
way `engineering.crypto`'s `bytea`-column callers do.

`ecc.domains.personal.domains` was the original, and is still the primary,
caller: `_encrypt_payload` encrypts a `sensitive`/`high_stakes` record_
type's listed fields before `INSERT`/`UPDATE`; `_decrypt_payload` is the
inverse, called only for the single-record response paths (create/get/
patch), never list/summary (`_redact_payload` handles those instead --
design doc Decision 3: "only a single-record fetch returns the decrypted
value"). Phase 10 Task 5 added two more direct callers for the identical
reason -- `gmail_adapter.py`'s `fetch_and_store_body` encrypts a
fetched Gmail message body before storing it in `email_messages.body`
(itself a `personal_domains`-owned, `high_stakes`-classified column, the
same tier `_encrypt_payload`'s own callers use), and `email_action_tools.
py`'s `get_thread_content_tool` decrypts it back for the AI runtime to
reason about -- both reusing this module's key rather than introducing a
second one for what is still the same personal-data encryption boundary.
"""

from base64 import urlsafe_b64encode
from functools import lru_cache
from hashlib import sha256

from cryptography.fernet import Fernet

from ecc.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    """Cached per-process, mirroring `ecc.domains.engineering.crypto.
    _fernet`'s identical reasoning and development-only fallback shape --
    see that function's own docstring for why the fallback is gated on the
    development marker rather than "key happens to be empty."
    """
    settings = get_settings()
    key = settings.personal_data_encryption_key
    if not key:
        if settings.environment.strip().casefold() != "development":
            raise RuntimeError(
                "ECC_PERSONAL_DATA_ENCRYPTION_KEY is unset outside development; "
                "ecc.config.validate_production_settings should have already rejected "
                "this at startup."
            )
        # Deterministic development-only fallback, derived from session_secret
        # with a distinct salt string from engineering.crypto's own fallback
        # so the two keys never collide even when both fall back in the same
        # unconfigured development environment.
        material = (settings.session_secret or "ecc-phase7-development-only-fallback").encode(
            "utf-8"
        ) + b"|personal-data"
        key = urlsafe_b64encode(sha256(material).digest()).decode("ascii")
    return Fernet(key.encode("ascii"))


def encrypt_field(plaintext: str) -> str:
    """`plaintext` is a single field's value -- a `domain_records.payload`
    field for this module's original caller, or (since Phase 10 Task 5) a
    Gmail message body stored directly in `email_messages.body`; see this
    module's own docstring for both callers. Returns Fernet's own
    URL-safe-base64 ciphertext token as a `str`, the exact value stored in
    place of the plaintext in either column.
    """
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_field(ciphertext: str) -> str:
    """Inverse of `encrypt_field`. Raises `cryptography.fernet.InvalidToken`
    if `ciphertext` was not produced by the currently configured key (e.g.
    `ECC_PERSONAL_DATA_ENCRYPTION_KEY` rotated without re-encrypting stored
    records) -- callers must treat that as a record needing re-encryption,
    not retry the decrypt.
    """
    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
