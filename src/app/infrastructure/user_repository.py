from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.user import AuthenticatedUser
from app.infrastructure.models import UserModel


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, api_key_hash: str) -> AuthenticatedUser:
        user = UserModel(api_key_hash=api_key_hash)
        self._session.add(user)
        await self._session.commit()
        await self._session.refresh(user)
        return AuthenticatedUser(id=user.id, balance=user.balance)

    async def get_by_api_key_hash(self, api_key_hash: str) -> AuthenticatedUser | None:
        user = await self._session.scalar(
            select(UserModel).where(UserModel.api_key_hash == api_key_hash)
        )
        if user is None:
            return None
        return AuthenticatedUser(id=user.id, balance=user.balance)

