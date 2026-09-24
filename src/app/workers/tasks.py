import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.application.providers import (
    GenerationProvider,
    PermanentProviderError,
    TemporaryProviderError,
)
from app.domain.generation import GenerationStatus
from app.infrastructure.database import async_session_factory
from app.infrastructure.generation_repository import SqlAlchemyGenerationRepository
from app.infrastructure.models import (
    CallbackOutboxModel,
    GenerationModel,
    GenerationOutboxModel,
)
from app.infrastructure.provider_factory import create_generation_provider
from app.settings import get_settings
from app.workers.celery_app import celery_app

CALLBACK_MAX_ATTEMPTS = 5
CALLBACK_RETRY_DELAY_SECONDS = 15
CALLBACK_DISPATCH_RESERVATION_SECONDS = 30


class CallbackDeliveryError(Exception):
    pass


class ClaimedTemporaryProviderError(TemporaryProviderError):
    def __init__(self, message: str, processing_token: UUID) -> None:
        super().__init__(message)
        self.processing_token = processing_token


class ClaimedPermanentProviderError(PermanentProviderError):
    def __init__(self, message: str, processing_token: UUID) -> None:
        super().__init__(message)
        self.processing_token = processing_token


class ClaimedUnexpectedProcessingError(Exception):
    def __init__(self, message: str, processing_token: UUID) -> None:
        super().__init__(message)
        self.processing_token = processing_token


async def process_generation_once(
    generation_id: UUID,
    session_factory=async_session_factory,
    provider: GenerationProvider | None = None,
) -> bool:
    settings = get_settings()
    async with session_factory() as session:
        repository = SqlAlchemyGenerationRepository(
            session, processing_lease_seconds=settings.processing_lease_seconds
        )
        generation = await repository.start_processing(generation_id)
    if generation is None:
        return False
    if generation.processing_token is None:  # pragma: no cover - database invariant
        raise RuntimeError("Claimed generation has no processing token")

    processing_token = generation.processing_token
    try:
        selected_provider = provider or create_generation_provider(
            settings, generation.provider
        )
        request_id = generation.provider_request_id
        if request_id is None:
            request_id = await selected_provider.submit(generation)
            async with session_factory() as session:
                repository = SqlAlchemyGenerationRepository(session)
                saved = await repository.save_provider_request(
                    generation_id,
                    processing_token=processing_token,
                    provider=selected_provider.name,
                    request_id=request_id,
                )
            if not saved:
                return False

        result = await selected_provider.get_result(generation, request_id)
        async with session_factory() as session:
            repository = SqlAlchemyGenerationRepository(session)
            return await repository.complete(
                generation_id,
                processing_token=processing_token,
                result=result.data,
                provider=result.provider,
            )
    except TemporaryProviderError as error:
        raise ClaimedTemporaryProviderError(str(error), processing_token) from error
    except PermanentProviderError as error:
        raise ClaimedPermanentProviderError(str(error), processing_token) from error
    except Exception as error:
        raise ClaimedUnexpectedProcessingError(str(error), processing_token) from error


@celery_app.task(
    bind=True,
    name="generation.process",
    ignore_result=True,
    max_retries=3,
)
def process_generation(self, generation_id: str) -> bool:
    parsed_id = UUID(generation_id)
    try:
        completed = asyncio.run(_process_with_worker_engine(parsed_id))
        return completed
    except ClaimedPermanentProviderError as error:
        asyncio.run(
            _fail_with_worker_engine(
                parsed_id,
                error.processing_token,
                "permanent_provider_error",
                str(error),
            )
        )
        return False
    except ClaimedTemporaryProviderError as error:
        if self.request.retries >= self.max_retries:
            failover_result = asyncio.run(
                _try_failover_with_worker_engine(
                    parsed_id, error.processing_token, str(error)
                )
            )
            if failover_result is not None:
                return failover_result
            asyncio.run(
                _fail_with_worker_engine(
                    parsed_id,
                    error.processing_token,
                    "temporary_provider_error",
                    str(error),
                )
            )
            return False
        asyncio.run(
            _requeue_with_worker_engine(
                parsed_id,
                error.processing_token,
                "temporary_provider_error",
                str(error),
            )
        )
        countdown = 5 * (2**self.request.retries)
        raise self.retry(exc=error, countdown=countdown) from error
    except ClaimedUnexpectedProcessingError as error:
        if self.request.retries >= self.max_retries:
            asyncio.run(
                _fail_with_worker_engine(
                    parsed_id,
                    error.processing_token,
                    "internal_processing_error",
                    str(error),
                )
            )
            return False
        asyncio.run(
            _requeue_with_worker_engine(
                parsed_id,
                error.processing_token,
                "internal_processing_error",
                str(error),
            )
        )
        countdown = 5 * (2**self.request.retries)
        raise self.retry(exc=error, countdown=countdown) from error


async def deliver_callback_once(
    generation_id: UUID,
    *,
    delivery_id: str,
    session_factory=async_session_factory,
    client: httpx.AsyncClient | None = None,
) -> bool:
    async with session_factory() as session:
        generation = await session.scalar(
            select(GenerationModel).where(
                GenerationModel.id == generation_id,
                GenerationModel.status.in_(
                    [GenerationStatus.COMPLETED.value, GenerationStatus.FAILED.value]
                ),
            )
        )
    if generation is None or generation.callback_url is None:
        return False

    payload = {
        "event": f"generation.{generation.status}",
        "generation_id": str(generation.id),
        "status": generation.status,
        "result": generation.result,
        "error": (
            {
                "code": generation.error_code,
                "message": generation.error_message,
            }
            if generation.status == GenerationStatus.FAILED.value
            else None
        ),
    }
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=get_settings().callback_timeout_seconds
    )
    try:
        response = await http_client.post(
            generation.callback_url,
            json=payload,
            headers={
                "X-Webhook-Event": payload["event"],
                "X-Webhook-Delivery": delivery_id,
            },
        )
        if not response.is_success:
            raise CallbackDeliveryError(
                f"Callback returned HTTP {response.status_code}"
            )
        return True
    except (httpx.TimeoutException, httpx.TransportError) as error:
        raise CallbackDeliveryError(str(error)) from error
    finally:
        if owns_client:
            await http_client.aclose()


async def claim_callback_delivery(
    event_id: UUID, session_factory=async_session_factory
) -> tuple[UUID, UUID] | None:
    now = datetime.now(UTC)
    claim_token = uuid.uuid4()
    in_flight_until = now + timedelta(
        seconds=get_settings().callback_delivery_lease_seconds
    )
    async with session_factory() as session:
        claimed = (
            await session.execute(
                update(CallbackOutboxModel)
                .where(
                    CallbackOutboxModel.id == event_id,
                    CallbackOutboxModel.delivered_at.is_(None),
                    CallbackOutboxModel.attempt_count < CALLBACK_MAX_ATTEMPTS,
                    CallbackOutboxModel.next_attempt_at <= now,
                    or_(
                        CallbackOutboxModel.in_flight_until.is_(None),
                        CallbackOutboxModel.in_flight_until < now,
                    ),
                )
                .values(
                    attempt_count=CallbackOutboxModel.attempt_count + 1,
                    claim_token=claim_token,
                    in_flight_until=in_flight_until,
                    dispatch_reserved_until=None,
                )
                .returning(
                    CallbackOutboxModel.generation_id,
                    CallbackOutboxModel.id,
                )
            )
        ).first()
        await session.commit()
    if claimed is None:
        return None
    return claimed.generation_id, claim_token


async def finish_callback_delivery(
    event_id: UUID,
    claim_token: UUID,
    *,
    delivered: bool,
    error_message: str | None,
    session_factory=async_session_factory,
) -> bool:
    values: dict = {
        "claim_token": None,
        "in_flight_until": None,
        "dispatch_reserved_until": None,
        "last_error": error_message[:2000] if error_message else None,
    }
    if delivered:
        values["delivered_at"] = func.now()
    else:
        values["next_attempt_at"] = datetime.now(UTC) + timedelta(
            seconds=CALLBACK_RETRY_DELAY_SECONDS
        )
    async with session_factory() as session:
        updated_id = await session.scalar(
            update(CallbackOutboxModel)
            .where(
                CallbackOutboxModel.id == event_id,
                CallbackOutboxModel.claim_token == claim_token,
            )
            .values(**values)
            .returning(CallbackOutboxModel.id)
        )
        await session.commit()
    return updated_id is not None


@celery_app.task(name="generation.deliver_callback", ignore_result=True)
def deliver_generation_callback(event_id: str) -> bool:
    parsed_event_id = UUID(event_id)
    claim = asyncio.run(_claim_callback_with_worker_engine(parsed_event_id))
    if claim is None:
        return False
    generation_id, claim_token = claim
    try:
        delivered = asyncio.run(
            _deliver_callback_with_worker_engine(
                generation_id, delivery_id=str(parsed_event_id)
            )
        )
    except CallbackDeliveryError as error:
        asyncio.run(
            _finish_callback_with_worker_engine(
                parsed_event_id,
                claim_token,
                delivered=False,
                error_message=str(error),
            )
        )
        return False
    asyncio.run(
        _finish_callback_with_worker_engine(
            parsed_event_id,
            claim_token,
            delivered=delivered,
            error_message=None,
        )
    )
    return delivered


async def _claim_callback_with_worker_engine(
    event_id: UUID,
) -> tuple[UUID, UUID] | None:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await claim_callback_delivery(event_id, session_factory)
    finally:
        await engine.dispose()


async def _finish_callback_with_worker_engine(
    event_id: UUID,
    claim_token: UUID,
    *,
    delivered: bool,
    error_message: str | None,
) -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await finish_callback_delivery(
            event_id,
            claim_token,
            delivered=delivered,
            error_message=error_message,
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()


async def _deliver_callback_with_worker_engine(
    generation_id: UUID, *, delivery_id: str
) -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await deliver_callback_once(
            generation_id,
            delivery_id=delivery_id,
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()


async def _process_with_worker_engine(
    generation_id: UUID, provider_name: str | None = None
) -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        provider = (
            create_generation_provider(get_settings(), provider_name)
            if provider_name is not None
            else None
        )
        return await process_generation_once(generation_id, session_factory, provider)
    finally:
        await engine.dispose()


async def _requeue_with_worker_engine(
    generation_id: UUID,
    processing_token: UUID,
    error_code: str,
    message: str,
) -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await SqlAlchemyGenerationRepository(session).requeue(
                generation_id,
                processing_token=processing_token,
                error_code=error_code,
                error_message=message,
            )
    finally:
        await engine.dispose()


async def _fail_with_worker_engine(
    generation_id: UUID,
    processing_token: UUID,
    error_code: str,
    message: str,
) -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await SqlAlchemyGenerationRepository(session).fail_and_refund(
                generation_id,
                processing_token=processing_token,
                error_code=error_code,
                error_message=message,
            )
    finally:
        await engine.dispose()


async def _try_failover_with_worker_engine(
    generation_id: UUID, processing_token: UUID, message: str
) -> bool | None:
    settings = get_settings()
    fallback_name = settings.fallback_generation_provider.strip()
    if not fallback_name or fallback_name == settings.generation_provider:
        return None

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            repository = SqlAlchemyGenerationRepository(session)
            request_id = await repository.get_provider_request_id(generation_id)
            if request_id is not None:
                return None
            requeued = await repository.requeue(
                generation_id,
                processing_token=processing_token,
                error_code="provider_failover",
                error_message=(
                    f"Primary provider unavailable; switching to {fallback_name}: {message}"
                ),
            )
        if not requeued:
            return None
        fallback = create_generation_provider(settings, fallback_name)
        try:
            return await process_generation_once(generation_id, session_factory, fallback)
        except (
            ClaimedTemporaryProviderError,
            ClaimedPermanentProviderError,
            ClaimedUnexpectedProcessingError,
        ) as fallback_error:
            async with session_factory() as session:
                await SqlAlchemyGenerationRepository(session).fail_and_refund(
                    generation_id,
                    processing_token=fallback_error.processing_token,
                    error_code="fallback_provider_error",
                    error_message=str(fallback_error),
                )
            return False
    finally:
        await engine.dispose()


async def dispatch_outbox_batch(
    limit: int = 20, session_factory=async_session_factory
) -> int:
    async with session_factory() as session:
        event_ids = list(
            await session.scalars(
                select(GenerationOutboxModel.id)
                .where(GenerationOutboxModel.processed_at.is_(None))
                .order_by(GenerationOutboxModel.created_at)
                .limit(limit)
            )
        )

    dispatched = 0
    for event_id in event_ids:
        try:
            async with session_factory() as session, session.begin():
                event = await session.scalar(
                    select(GenerationOutboxModel)
                    .where(
                        GenerationOutboxModel.id == event_id,
                        GenerationOutboxModel.processed_at.is_(None),
                    )
                    .with_for_update(skip_locked=True)
                )
                if event is None:
                    continue
                await session.execute(
                    update(GenerationModel)
                    .where(
                        GenerationModel.id == event.generation_id,
                        GenerationModel.status == GenerationStatus.CREATED.value,
                    )
                    .values(status=GenerationStatus.QUEUED.value, updated_at=func.now())
                )
                process_generation.apply_async(args=[str(event.generation_id)])
                event.processed_at = func.now()
                event.attempt_count += 1
                dispatched += 1
        except Exception as error:
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(GenerationOutboxModel)
                    .where(GenerationOutboxModel.id == event_id)
                    .values(
                        attempt_count=GenerationOutboxModel.attempt_count + 1,
                        last_error=str(error)[:2000],
                    )
                )
    return dispatched


async def recover_stale_generations_batch(
    limit: int = 20, session_factory=async_session_factory
) -> int:
    now = datetime.now(UTC)
    stale_queued_before = now - timedelta(
        seconds=get_settings().queued_recovery_seconds
    )
    async with session_factory() as session, session.begin():
        stale_rows = (
            await session.execute(
                select(GenerationModel.id, GenerationModel.status)
                .where(
                    or_(
                        (
                            (GenerationModel.status == GenerationStatus.PROCESSING.value)
                            & GenerationModel.lease_expires_at.is_not(None)
                            & (GenerationModel.lease_expires_at < now)
                        ),
                        (
                            (GenerationModel.status == GenerationStatus.QUEUED.value)
                            & (GenerationModel.updated_at < stale_queued_before)
                        ),
                    )
                )
                .order_by(GenerationModel.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        generation_ids = [row.id for row in stale_rows]
        if not generation_ids:
            return 0
        expired_processing_ids = [
            row.id
            for row in stale_rows
            if row.status == GenerationStatus.PROCESSING.value
        ]
        if expired_processing_ids:
            await session.execute(
                update(GenerationModel)
                .where(
                    GenerationModel.id.in_(expired_processing_ids),
                    GenerationModel.status == GenerationStatus.PROCESSING.value,
                    GenerationModel.lease_expires_at < now,
                )
                .values(
                    status=GenerationStatus.QUEUED.value,
                    processing_token=None,
                    lease_expires_at=None,
                    error_code="worker_lease_expired",
                    error_message="Worker lease expired; generation was queued for recovery",
                    updated_at=func.now(),
                )
            )
        await session.execute(
            update(GenerationOutboxModel)
            .where(GenerationOutboxModel.generation_id.in_(generation_ids))
            .values(
                processed_at=None,
                last_error="Generation requeued after worker lease expired",
            )
        )
    return len(generation_ids)


async def dispatch_callback_outbox_batch(
    limit: int = 20, session_factory=async_session_factory
) -> int:
    now = datetime.now(UTC)
    async with session_factory() as session:
        event_ids = list(
            await session.scalars(
                select(CallbackOutboxModel.id)
                .where(
                    CallbackOutboxModel.delivered_at.is_(None),
                    CallbackOutboxModel.attempt_count < CALLBACK_MAX_ATTEMPTS,
                    CallbackOutboxModel.next_attempt_at <= now,
                    or_(
                        CallbackOutboxModel.in_flight_until.is_(None),
                        CallbackOutboxModel.in_flight_until < now,
                    ),
                    or_(
                        CallbackOutboxModel.dispatch_reserved_until.is_(None),
                        CallbackOutboxModel.dispatch_reserved_until < now,
                    ),
                )
                .order_by(CallbackOutboxModel.next_attempt_at)
                .limit(limit)
            )
        )

    dispatched = 0
    for event_id in event_ids:
        try:
            async with session_factory() as session, session.begin():
                event = await session.scalar(
                    select(CallbackOutboxModel)
                    .where(
                        CallbackOutboxModel.id == event_id,
                        CallbackOutboxModel.delivered_at.is_(None),
                        CallbackOutboxModel.attempt_count < CALLBACK_MAX_ATTEMPTS,
                        CallbackOutboxModel.next_attempt_at <= now,
                        or_(
                            CallbackOutboxModel.in_flight_until.is_(None),
                            CallbackOutboxModel.in_flight_until < now,
                        ),
                        or_(
                            CallbackOutboxModel.dispatch_reserved_until.is_(None),
                            CallbackOutboxModel.dispatch_reserved_until < now,
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
                if event is None:
                    continue
                event.dispatch_reserved_until = now + timedelta(
                    seconds=CALLBACK_DISPATCH_RESERVATION_SECONDS
                )
                deliver_generation_callback.apply_async(args=[str(event.id)])
                dispatched += 1
        except Exception as error:
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(CallbackOutboxModel)
                    .where(CallbackOutboxModel.id == event_id)
                    .values(last_error=str(error)[:2000], dispatch_reserved_until=None)
                )
    return dispatched


@celery_app.task(name="generation.dispatch_outbox", ignore_result=True)
def dispatch_outbox() -> int:
    return asyncio.run(_dispatch_with_worker_engine())


async def _dispatch_with_worker_engine() -> int:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await dispatch_outbox_batch(session_factory=session_factory)
    finally:
        await engine.dispose()


@celery_app.task(name="generation.recover_stale", ignore_result=True)
def recover_stale_generations() -> int:
    return asyncio.run(_recover_stale_with_worker_engine())


async def _recover_stale_with_worker_engine() -> int:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await recover_stale_generations_batch(session_factory=session_factory)
    finally:
        await engine.dispose()


@celery_app.task(name="generation.dispatch_callback_outbox", ignore_result=True)
def dispatch_callback_outbox() -> int:
    return asyncio.run(_dispatch_callback_with_worker_engine())


async def _dispatch_callback_with_worker_engine() -> int:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await dispatch_callback_outbox_batch(session_factory=session_factory)
    finally:
        await engine.dispose()
