from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.payment import (
    PaymentConflictError,
    PaymentResult,
    PaymentStatus,
    PaymentUserNotFoundError,
)
from app.infrastructure.models import PaymentEventModel, UserModel


class SqlAlchemyPaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def apply_credit(
        self, *, external_id: str, user_id: UUID, amount: int
    ) -> PaymentResult:
        user_exists = await self._session.scalar(
            select(UserModel.id).where(UserModel.id == user_id)
        )
        if user_exists is None:
            raise PaymentUserNotFoundError

        values = {"external_id": external_id, "user_id": user_id, "amount": amount}
        dialect_name = self._session.bind.dialect.name if self._session.bind else ""
        if dialect_name == "postgresql":
            statement = postgresql_insert(PaymentEventModel).values(**values)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(PaymentEventModel).values(**values)
        else:  # pragma: no cover - only PostgreSQL and SQLite are supported
            raise RuntimeError(f"Unsupported database dialect: {dialect_name}")

        inserted_event_id = await self._session.scalar(
            statement.on_conflict_do_nothing(index_elements=["external_id"]).returning(
                PaymentEventModel.id
            )
        )

        if inserted_event_id is None:
            existing = await self._session.scalar(
                select(PaymentEventModel).where(PaymentEventModel.external_id == external_id)
            )
            if existing is None or existing.user_id != user_id or existing.amount != amount:
                await self._session.rollback()
                raise PaymentConflictError
            balance = await self._session.scalar(
                select(UserModel.balance).where(UserModel.id == user_id)
            )
            await self._session.commit()
            return PaymentResult(status=PaymentStatus.DUPLICATE, balance=balance or 0)

        balance = await self._session.scalar(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(balance=UserModel.balance + amount)
            .returning(UserModel.balance)
        )
        if balance is None:  # protects against a future user-deletion feature
            await self._session.rollback()
            raise PaymentUserNotFoundError

        await self._session.commit()
        return PaymentResult(status=PaymentStatus.APPLIED, balance=balance)

