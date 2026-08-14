"""Shared HMAC-signed opaque pagination cursor -- extracted from 13
near-identical `_encode_cursor`/`_decode_cursor` pairs across attention/
calendar/scheduling/planning/knowledge/governance/communication routers
(Loop 2 architecture review's "HMAC cursor pagination dedup" finding).
Each caller encoded/decoded a different field name (`created_at`,
`updated_at`, `starts_at`, `effective_at`, `score`, ...) alongside `id`,
but with the identical construction: JSON-encode the fields, HMAC-SHA256
sign with the session secret, base64url the `payload + b"." + hex_
signature` concatenation.

**`search.py`'s own cursor is a deliberately different construction**
(raw-binary signature appended with no delimiter, located by a fixed
32-byte slice, not `.`-split hex) and is NOT migrated to this module --
folding it in would silently change its wire format. See that module's
own cursor functions.

`decode_cursor` raises `HTTPException(400, detail)` itself for a
b64/signature/JSON failure, `detail` defaulting to `"MALFORMED_CURSOR"`
-- the string every caller but two used already. `attention/planning.py`/
`attention/waiting.py` pass `detail="CURSOR_INVALID"` to keep their own
pre-existing (and test-asserted) detail string for a forged/corrupt
cursor. Field-level decoding (e.g. `datetime.fromisoformat`, `UUID(...)`)
stays the caller's own responsibility -- each caller's own `_decode_
cursor` wraps its field extraction in a `try/except (ValueError,
KeyError, TypeError) as exc: raise HTTPException(400, detail) from exc`,
matching the SAME detail string passed to `decode_cursor` above, so a
malformed field value degrades identically to a forged/corrupt cursor.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from hmac import compare_digest, new
from json import dumps, loads
from typing import Any

from fastapi import HTTPException

from ecc.config import get_settings


def encode_cursor(fields: dict[str, Any]) -> str:
    payload = dumps(fields, separators=(",", ":")).encode()
    signature = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def decode_cursor(cursor: str, *, detail: str = "MALFORMED_CURSOR") -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = urlsafe_b64decode(padded.encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded: dict[str, Any] = loads(payload)
        return decoded
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=detail) from exc
