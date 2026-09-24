from functools import lru_cache
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import UserRepository, authenticate_user
from app.application.generations import GenerationRepository
from app.application.payments import PaymentRepository
from app.domain.user import AuthenticatedUser
from app.infrastructure.database import get_session
from app.infrastructure.generation_repository import SqlAlchemyGenerationRepository
from app.infrastructure.payment_repository import SqlAlchemyPaymentRepository
from app.infrastructure.rate_limiter import RateLimitDecision, RedisRateLimiter
from app.infrastructure.user_repository import SqlAlchemyUserRepository
from app.settings import Settings, get_settings

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_user_repository(session: SessionDep) -> UserRepository:
    return SqlAlchemyUserRepository(session)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]


def get_payment_repository(session: SessionDep) -> PaymentRepository:
    return SqlAlchemyPaymentRepository(session)


PaymentRepositoryDep = Annotated[PaymentRepository, Depends(get_payment_repository)]


def get_generation_repository(session: SessionDep) -> GenerationRepository:
    return SqlAlchemyGenerationRepository(session)


GenerationRepositoryDep = Annotated[GenerationRepository, Depends(get_generation_repository)]


async def get_current_user(
    repository: UserRepositoryDep,
    settings: SettingsDep,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> AuthenticatedUser:
    if api_key is not None:
        user = await authenticate_user(repository, api_key, settings)
        if user is not None:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]


class RateLimiter(Protocol):
    async def check(self, user_id: UUID) -> RateLimitDecision: ...


@lru_cache
def get_rate_limiter() -> RedisRateLimiter:
    settings = get_settings()
    return RedisRateLimiter(
        Redis.from_url(settings.redis_url, decode_responses=True),
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
        block_seconds=settings.rate_limit_block_seconds,
    )


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


async def enforce_rate_limit(
    current_user: CurrentUserDep,
    limiter: RateLimiterDep,
) -> None:
    decision = await limiter.check(current_user.id)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after)},
        )


RateLimitDep = Annotated[None, Depends(enforce_rate_limit)]
