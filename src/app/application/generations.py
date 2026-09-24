import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.domain.generation import Generation, GenerationKind
from app.settings import Settings


@dataclass(frozen=True, slots=True)
class CreateGenerationCommand:
    user_id: UUID
    idempotency_key: str
    kind: GenerationKind
    prompt: str
    source_url: str | None
    callback_url: str | None
    input_params: dict[str, Any]


class GenerationRepository(Protocol):
    async def create_and_charge(
        self,
        *,
        command: CreateGenerationCommand,
        request_hash: str,
        cost: int,
    ) -> Generation: ...

    async def get_for_user(self, generation_id: UUID, user_id: UUID) -> Generation | None: ...

    async def start_processing(self, generation_id: UUID) -> Generation | None: ...

    async def complete(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        result: dict,
        provider: str,
    ) -> bool: ...

    async def save_provider_request(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        provider: str,
        request_id: str,
    ) -> bool: ...

    async def requeue(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        error_code: str,
        error_message: str,
    ) -> bool: ...

    async def fail_and_refund(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        error_code: str,
        error_message: str,
    ) -> bool: ...

    async def get_provider_request_id(self, generation_id: UUID) -> str | None: ...



def generation_cost(
    kind: GenerationKind, settings: Settings, input_params: dict[str, Any]
) -> int:
    base_cost = {
        GenerationKind.TEXT_TO_IMAGE: settings.cost_text_to_image,
        GenerationKind.IMAGE_TO_IMAGE: settings.cost_image_to_image,
        GenerationKind.TEXT_TO_VIDEO: settings.cost_text_to_video,
        GenerationKind.IMAGE_TO_VIDEO: settings.cost_image_to_video,
    }[kind]
    if kind in {GenerationKind.TEXT_TO_IMAGE, GenerationKind.IMAGE_TO_IMAGE}:
        return base_cost * int(input_params.get("num_images", 1))

    duration_multiplier = int(input_params.get("duration", "5")) / 5
    resolution = str(input_params.get("resolution", "1080p"))
    resolution_percent = {
        "480p": settings.video_resolution_480p_percent,
        "720p": settings.video_resolution_720p_percent,
        "1080p": settings.video_resolution_1080p_percent,
    }[resolution]
    return math.ceil(base_cost * duration_multiplier * resolution_percent / 100)


def request_fingerprint(command: CreateGenerationCommand) -> str:
    payload = {
        "kind": command.kind.value,
        "prompt": command.prompt,
        "source_url": command.source_url,
        "callback_url": command.callback_url,
        "input_params": command.input_params,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def create_generation(
    repository: GenerationRepository,
    command: CreateGenerationCommand,
    settings: Settings,
) -> Generation:
    cost = generation_cost(command.kind, settings, command.input_params)
    return await repository.create_and_charge(
        command=command,
        request_hash=request_fingerprint(command),
        cost=cost,
    )
