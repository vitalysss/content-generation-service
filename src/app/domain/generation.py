from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class GenerationKind(StrEnum):
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"
    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"


class GenerationStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Generation:
    id: UUID
    user_id: UUID
    kind: GenerationKind
    status: GenerationStatus
    prompt: str
    source_url: str | None
    callback_url: str | None
    input_params: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    cost: int
    provider: str | None
    provider_request_id: str | None
    processing_token: UUID | None
    processing_started_at: datetime | None
    lease_expires_at: datetime | None
    attempt_count: int
    created_at: datetime
    updated_at: datetime


class InsufficientBalanceError(Exception):
    pass


class GenerationIdempotencyConflictError(Exception):
    pass
