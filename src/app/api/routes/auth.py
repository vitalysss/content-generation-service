from fastapi import APIRouter, status

from app.api.dependencies import (
    CurrentUserDep,
    RateLimitDep,
    SettingsDep,
    UserRepositoryDep,
)
from app.api.schemas.auth import AuthResponse, MeResponse
from app.application.auth import create_user

router = APIRouter(tags=["auth"])


@router.post("/auth", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(repository: UserRepositoryDep, settings: SettingsDep) -> AuthResponse:
    user, api_key = await create_user(repository, settings)
    return AuthResponse(user_id=user.id, api_key=api_key, balance=user.balance)


@router.get("/me", response_model=MeResponse)
async def get_me(current_user: CurrentUserDep, _rate_limit: RateLimitDep) -> MeResponse:
    return MeResponse(user_id=current_user.id, balance=current_user.balance)
