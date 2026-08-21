"""Shared empty-body request model.

Every action-only endpoint (no real request payload) still needs a
Pydantic model to feed ecc.platform.idempotency.request_hash -- this was
copy-pasted as an identical private ``_EmptyBody`` stub in 12 domain files.
"""

from pydantic import BaseModel, ConfigDict


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
