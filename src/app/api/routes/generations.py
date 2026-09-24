from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status

from app.api.dependencies import (
    CurrentUserDep,
    GenerationRepositoryDep,
    RateLimitDep,
    SettingsDep,
)
from app.api.schemas.generations import CreateGenerationRequest, GenerationResponse
from app.application.generations import CreateGenerationCommand, create_generation
from app.domain.generation import (
    GenerationIdempotencyConflictError,
    InsufficientBalanceError,
)

router = APIRouter(prefix="/generations", tags=["generations"])


@router.post("", response_model=GenerationResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_generation(
    payload: CreateGenerationRequest,
    current_user: CurrentUserDep,
    _rate_limit: RateLimitDep,
    repository: GenerationRepositoryDep,
    settings: SettingsDep,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ],
) -> GenerationResponse:
    command = CreateGenerationCommand(
        user_id=current_user.id,
        idempotency_key=idempotency_key,
        kind=payload.kind,
        prompt=payload.prompt,
        source_url=str(payload.source_url) if payload.source_url else None,
        callback_url=str(payload.callback_url) if payload.callback_url else None,
        input_params=payload.parameters,
    )
    try:
        generation = await create_generation(repository, command, settings)
    except InsufficientBalanceError as error:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Insufficient balance",
        ) from error
    except GenerationIdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used with a different request",
        ) from error
    return GenerationResponse.model_validate(generation)


@router.get("/{generation_id}", response_model=GenerationResponse)
async def get_generation(
    generation_id: UUID,
    current_user: CurrentUserDep,
    _rate_limit: RateLimitDep,
    repository: GenerationRepositoryDep,
) -> GenerationResponse:
    generation = await repository.get_for_user(generation_id, current_user.id)
    if generation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation not found")
    return GenerationResponse.model_validate(generation)
