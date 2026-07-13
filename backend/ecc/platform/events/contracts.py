from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    event_type: str
    event_version: int = 1
    occurred_at: datetime
    workspace_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    producer: str
    payload: dict[str, Any]
