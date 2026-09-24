import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.generations import CreateGenerationCommand
from app.domain.generation import (
    Generation,
    GenerationIdempotencyConflictError,
    GenerationKind,
    GenerationStatus,
    InsufficientBalanceError,
)
from app.infrastructure.models import (
    CallbackOutboxModel,
    GenerationModel,
    GenerationOutboxModel,
    UserModel,
)


def _to_domain(model: GenerationModel) -> Generation:
    return Generation(
        id=model.id,
        user_id=model.user_id,
        kind=GenerationKind(model.kind),
        status=GenerationStatus(model.status),
        prompt=model.prompt,
        source_url=model.source_url,
        callback_url=model.callback_url,
        input_params=model.input_params,
        result=model.result,
        error_code=model.error_code,
        error_message=model.error_message,
        cost=model.cost,
        provider=model.provider,
        provider_request_id=model.provider_request_id,
        processing_token=model.processing_token,
        processing_started_at=model.processing_started_at,
        lease_expires_at=model.lease_expires_at,
        attempt_count=model.attempt_count,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyGenerationRepository:
    def __init__(self, session: AsyncSession, *, processing_lease_seconds: int = 600) -> None:
        self._session = session
        self._processing_lease_seconds = processing_lease_seconds

    async def create_and_charge(
        self,
        *,
        command: CreateGenerationCommand,
        request_hash: str,
        cost: int,
    ) -> Generation:
        generation_id = uuid.uuid4()
        values = {
            "id": generation_id,
            "user_id": command.user_id,
            "idempotency_key": command.idempotency_key,
            "request_hash": request_hash,
            "kind": command.kind.value,
            "status": GenerationStatus.CREATED.value,
            "prompt": command.prompt,
            "source_url": command.source_url,
            "callback_url": command.callback_url,
            "input_params": command.input_params,
            "cost": cost,
        }
        dialect_name = self._session.bind.dialect.name if self._session.bind else ""
        if dialect_name == "postgresql":
            statement = postgresql_insert(GenerationModel).values(**values)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(GenerationModel).values(**values)
        else:  # pragma: no cover - only PostgreSQL and SQLite are supported
            raise RuntimeError(f"Unsupported database dialect: {dialect_name}")

        inserted_id = await self._session.scalar(
            statement.on_conflict_do_nothing(
                index_elements=["user_id", "idempotency_key"]
            ).returning(GenerationModel.id)
        )
        if inserted_id is None:
            existing = await self._session.scalar(
                select(GenerationModel).where(
                    GenerationModel.user_id == command.user_id,
                    GenerationModel.idempotency_key == command.idempotency_key,
                )
            )
            if existing is None or existing.request_hash != request_hash:
                await self._session.rollback()
                raise GenerationIdempotencyConflictError
            await self._session.commit()
            return _to_domain(existing)

        remaining_balance = await self._session.scalar(
            update(UserModel)
            .where(UserModel.id == command.user_id, UserModel.balance >= cost)
            .values(balance=UserModel.balance - cost)
            .returning(UserModel.balance)
        )
        if remaining_balance is None:
            await self._session.rollback()
            raise InsufficientBalanceError

        self._session.add(GenerationOutboxModel(generation_id=generation_id))

        model = await self._session.scalar(
            select(GenerationModel).where(GenerationModel.id == generation_id)
        )
        if model is None:  # pragma: no cover - guarded by the transaction above
            await self._session.rollback()
            raise RuntimeError("Generation disappeared during creation")
        await self._session.commit()
        return _to_domain(model)

    async def get_for_user(self, generation_id: UUID, user_id: UUID) -> Generation | None:
        model = await self._session.scalar(
            select(GenerationModel).where(
                GenerationModel.id == generation_id,
                GenerationModel.user_id == user_id,
            )
        )
        return _to_domain(model) if model is not None else None

    async def start_processing(self, generation_id: UUID) -> Generation | None:
        processing_token = uuid.uuid4()
        lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=self._processing_lease_seconds
        )
        claimed_id = await self._session.scalar(
            update(GenerationModel)
            .where(
                GenerationModel.id == generation_id,
                or_(
                    GenerationModel.status.in_(
                        [GenerationStatus.CREATED.value, GenerationStatus.QUEUED.value]
                    ),
                    (
                        (GenerationModel.status == GenerationStatus.PROCESSING.value)
                        & (GenerationModel.lease_expires_at < func.now())
                    ),
                ),
            )
            .values(
                status=GenerationStatus.PROCESSING.value,
                processing_token=processing_token,
                processing_started_at=func.now(),
                lease_expires_at=lease_expires_at,
                attempt_count=GenerationModel.attempt_count + 1,
                updated_at=func.now(),
            )
            .returning(GenerationModel.id)
        )
        if claimed_id is None:
            await self._session.rollback()
            return None
        model = await self._session.scalar(
            select(GenerationModel).where(GenerationModel.id == generation_id)
        )
        await self._session.commit()
        return _to_domain(model) if model is not None else None

    async def complete(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        result: dict,
        provider: str,
    ) -> bool:
        completed = (
            await self._session.execute(
                update(GenerationModel)
                .where(
                    GenerationModel.id == generation_id,
                    GenerationModel.status == GenerationStatus.PROCESSING.value,
                    GenerationModel.processing_token == processing_token,
                )
                .values(
                    status=GenerationStatus.COMPLETED.value,
                    result=result,
                    provider=provider,
                    processing_token=None,
                    lease_expires_at=None,
                    completed_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(GenerationModel.id, GenerationModel.callback_url)
            )
        ).first()
        if completed is not None and completed.callback_url is not None:
            self._session.add(CallbackOutboxModel(generation_id=completed.id))
        await self._session.commit()
        return completed is not None

    async def save_provider_request(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        provider: str,
        request_id: str,
    ) -> bool:
        saved_id = await self._session.scalar(
            update(GenerationModel)
            .where(
                GenerationModel.id == generation_id,
                GenerationModel.status == GenerationStatus.PROCESSING.value,
                GenerationModel.processing_token == processing_token,
                GenerationModel.provider_request_id.is_(None),
            )
            .values(provider=provider, provider_request_id=request_id, updated_at=func.now())
            .returning(GenerationModel.id)
        )
        await self._session.commit()
        return saved_id is not None

    async def requeue(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        error_code: str,
        error_message: str,
    ) -> bool:
        queued_id = await self._session.scalar(
            update(GenerationModel)
            .where(
                GenerationModel.id == generation_id,
                GenerationModel.status == GenerationStatus.PROCESSING.value,
                GenerationModel.processing_token == processing_token,
            )
            .values(
                status=GenerationStatus.QUEUED.value,
                error_code=error_code,
                error_message=error_message[:2000],
                processing_token=None,
                lease_expires_at=None,
                updated_at=func.now(),
            )
            .returning(GenerationModel.id)
        )
        await self._session.commit()
        return queued_id is not None

    async def fail_and_refund(
        self,
        generation_id: UUID,
        *,
        processing_token: UUID,
        error_code: str,
        error_message: str,
    ) -> bool:
        failed = (
            await self._session.execute(
                update(GenerationModel)
                .where(
                    GenerationModel.id == generation_id,
                    GenerationModel.status.in_(
                        [GenerationStatus.QUEUED.value, GenerationStatus.PROCESSING.value]
                    ),
                    GenerationModel.processing_token == processing_token,
                    GenerationModel.refunded_at.is_(None),
                )
                .values(
                    status=GenerationStatus.FAILED.value,
                    error_code=error_code,
                    error_message=error_message[:2000],
                    processing_token=None,
                    lease_expires_at=None,
                    refunded_at=func.now(),
                    completed_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(
                    GenerationModel.id,
                    GenerationModel.user_id,
                    GenerationModel.cost,
                    GenerationModel.callback_url,
                )
            )
        ).first()
        if failed is None:
            await self._session.rollback()
            return False
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == failed.user_id)
            .values(balance=UserModel.balance + failed.cost)
        )
        if failed.callback_url is not None:
            self._session.add(CallbackOutboxModel(generation_id=failed.id))
        await self._session.commit()
        return True

    async def get_provider_request_id(self, generation_id: UUID) -> str | None:
        return await self._session.scalar(
            select(GenerationModel.provider_request_id).where(
                GenerationModel.id == generation_id
            )
        )
