import json
from datetime import UTC, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.workers.tasks as worker_tasks
from app.api.dependencies import get_rate_limiter
from app.application.providers import ProviderResult, TemporaryProviderError
from app.infrastructure.database import Base, get_session
from app.infrastructure.fake_provider import FakeGenerationProvider
from app.infrastructure.generation_repository import SqlAlchemyGenerationRepository
from app.infrastructure.models import (
    CallbackOutboxModel,
    GenerationModel,
    GenerationOutboxModel,
    UserModel,
)
from app.infrastructure.observability import configure_request_logging
from app.infrastructure.rate_limiter import RateLimitDecision
from app.main import app
from app.settings import get_settings
from app.workers.tasks import (
    CALLBACK_MAX_ATTEMPTS,
    CALLBACK_RETRY_DELAY_SECONDS,
    CallbackDeliveryError,
    claim_callback_delivery,
    deliver_callback_once,
    deliver_generation_callback,
    dispatch_callback_outbox_batch,
    dispatch_outbox_batch,
    finish_callback_delivery,
    process_generation,
    process_generation_once,
    recover_stale_generations_batch,
)


class FlakyProvider:
    name = "fake"

    def __init__(self) -> None:
        self.submit_count = 0
        self.result_count = 0

    async def submit(self, generation) -> str:
        self.submit_count += 1
        return "durable-request-id"

    async def get_result(self, generation, request_id: str) -> ProviderResult:
        self.result_count += 1
        if self.result_count == 1:
            raise TemporaryProviderError("temporary outage")
        return ProviderResult(
            provider=self.name,
            data={"url": "https://example.invalid/recovered.png"},
        )


class UnavailableBeforeSubmissionProvider:
    name = "fal"

    async def submit(self, generation) -> str:
        raise TemporaryProviderError("primary is unavailable")

    async def get_result(self, generation, request_id: str) -> ProviderResult:
        raise AssertionError("No request was submitted")


class FakeRateLimiter:
    def __init__(self, limit: int = 10) -> None:
        self._limit = limit
        self._counts: dict[UUID, int] = {}
        self._blocked: set[UUID] = set()

    async def check(self, user_id: UUID) -> RateLimitDecision:
        if user_id in self._blocked:
            return RateLimitDecision(allowed=False, retry_after=60)
        count = self._counts.get(user_id, 0) + 1
        self._counts[user_id] = count
        if count > self._limit:
            self._blocked.add(user_id)
            return RateLimitDecision(allowed=False, retry_after=60)
        return RateLimitDecision(allowed=True, retry_after=60)


async def register_and_fund(test_client, amount: int = 500) -> dict:
    created = (await test_client.post("/v1/auth")).json()
    secret = get_settings().payment_webhook_secret.get_secret_value()
    response = await test_client.post(
        "/v1/payments/webhook",
        headers={"X-Payment-Secret": secret},
        json={
            "external_id": f"fund-{created['user_id']}",
            "user_id": created["user_id"],
            "amount": amount,
        },
    )
    assert response.status_code == 200
    return created


@pytest_asyncio.fixture
async def client():
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_session():
        async with test_sessions() as session:
            yield session

    fake_rate_limiter = FakeRateLimiter()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_rate_limiter] = lambda: fake_rate_limiter
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client, test_sessions

    app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_health(client) -> None:
    test_client, _ = client
    response = await test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_request_id_metrics_and_log_rotation(client) -> None:
    test_client, _ = client

    health = await test_client.get(
        "/health", headers={"X-Request-ID": "request-from-client"}
    )
    metrics = await test_client.get("/metrics")

    assert health.headers["X-Request-ID"] == "request-from-client"
    assert metrics.status_code == 200
    assert "content_service_http_requests_total" in metrics.text
    assert 'route="/health"' in metrics.text
    logger = configure_request_logging(get_settings())
    rotating_handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, TimedRotatingFileHandler)
    ]
    assert len(rotating_handlers) == 1
    assert rotating_handlers[0].backupCount == 7


@pytest.mark.asyncio
async def test_register_and_authenticate(client) -> None:
    test_client, test_sessions = client

    created = await test_client.post("/v1/auth")
    assert created.status_code == 201
    payload = created.json()
    assert payload["api_key"].startswith("cgs_")
    assert payload["balance"] == 0

    me = await test_client.get("/v1/me", headers={"X-API-Key": payload["api_key"]})
    assert me.status_code == 200
    assert me.json() == {"user_id": payload["user_id"], "balance": 0}

    async with test_sessions() as session:
        stored_user = await session.scalar(select(UserModel))
    assert stored_user is not None
    assert stored_user.api_key_hash != payload["api_key"]
    assert len(stored_user.api_key_hash) == 64


@pytest.mark.asyncio
async def test_rejects_invalid_api_key(client) -> None:
    test_client, _ = client
    response = await test_client.get("/v1/me", headers={"X-API-Key": "cgs_invalid"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rate_limit_blocks_eleventh_authenticated_request(client) -> None:
    test_client, _ = client
    user = (await test_client.post("/v1/auth")).json()
    headers = {"X-API-Key": user["api_key"]}

    allowed = [await test_client.get("/v1/me", headers=headers) for _ in range(10)]
    blocked = await test_client.get("/v1/me", headers=headers)
    still_blocked = await test_client.get("/v1/me", headers=headers)

    assert all(response.status_code == 200 for response in allowed)
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Rate limit exceeded"}
    assert blocked.headers["Retry-After"] == "60"
    assert still_blocked.status_code == 429


@pytest.mark.asyncio
async def test_payment_webhook_is_idempotent(client) -> None:
    test_client, _ = client
    created = (await test_client.post("/v1/auth")).json()
    secret = get_settings().payment_webhook_secret.get_secret_value()
    headers = {"X-Payment-Secret": secret}
    payment = {
        "external_id": "payment-001",
        "user_id": created["user_id"],
        "amount": 75,
    }

    applied = await test_client.post("/v1/payments/webhook", json=payment, headers=headers)
    duplicate = await test_client.post("/v1/payments/webhook", json=payment, headers=headers)
    me = await test_client.get("/v1/me", headers={"X-API-Key": created["api_key"]})

    assert applied.status_code == 200
    assert applied.json() == {"status": "applied", "balance": 75}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate", "balance": 75}
    assert me.json()["balance"] == 75


@pytest.mark.asyncio
async def test_payment_webhook_rejects_conflicting_duplicate(client) -> None:
    test_client, _ = client
    created = (await test_client.post("/v1/auth")).json()
    headers = {
        "X-Payment-Secret": get_settings().payment_webhook_secret.get_secret_value()
    }
    payment = {
        "external_id": "payment-conflict",
        "user_id": created["user_id"],
        "amount": 10,
    }

    first = await test_client.post("/v1/payments/webhook", json=payment, headers=headers)
    payment["amount"] = 20
    conflict = await test_client.post("/v1/payments/webhook", json=payment, headers=headers)

    assert first.status_code == 200
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_payment_webhook_requires_secret(client) -> None:
    test_client, _ = client
    response = await test_client.post(
        "/v1/payments/webhook",
        json={
            "external_id": "payment-unauthorized",
            "user_id": "00000000-0000-0000-0000-000000000001",
            "amount": 10,
        },
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_payment_webhook_rejects_unknown_user(client) -> None:
    test_client, _ = client
    response = await test_client.post(
        "/v1/payments/webhook",
        headers={
            "X-Payment-Secret": get_settings().payment_webhook_secret.get_secret_value()
        },
        json={
            "external_id": "unknown-user-payment",
            "user_id": "00000000-0000-0000-0000-000000000001",
            "amount": 10,
        },
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_payment_webhook_accepts_assignment_contract(client) -> None:
    test_client, _ = client
    created = (await test_client.post("/v1/auth")).json()
    secret = get_settings().payment_webhook_secret.get_secret_value()
    payment = {
        "external_user_id": created["user_id"],
        "amount": 100,
    }

    applied = await test_client.post(
        "/v1/payments/webhook",
        json=payment,
        headers={"X-Payment-Secret": secret},
    )
    duplicate = await test_client.post(
        "/v1/payments/webhook",
        json=payment,
        headers={"X-Payment-Secret": secret},
    )

    assert applied.status_code == 200
    assert applied.json() == {"status": "applied", "balance": 100}
    assert duplicate.json() == {"status": "duplicate", "balance": 100}


@pytest.mark.asyncio
async def test_payment_webhook_requires_event_id_in_production(client) -> None:
    test_client, _ = client
    created = (await test_client.post("/v1/auth")).json()
    settings = get_settings()
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"app_env": "production"}
    )
    headers = {"X-Payment-Secret": settings.payment_webhook_secret.get_secret_value()}
    payment = {"external_user_id": created["user_id"], "amount": 100}

    missing_id = await test_client.post(
        "/v1/payments/webhook", json=payment, headers=headers
    )
    conflicting_id = await test_client.post(
        "/v1/payments/webhook",
        json={**payment, "external_id": "body-id"},
        headers={**headers, "X-Payment-Event-ID": "header-id"},
    )

    assert missing_id.status_code == 400
    assert conflicting_id.status_code == 409


@pytest.mark.asyncio
async def test_generation_not_found(client) -> None:
    test_client, _ = client
    user = (await test_client.post("/v1/auth")).json()
    response = await test_client.get(
        "/v1/generations/00000000-0000-0000-0000-000000000001",
        headers={"X-API-Key": user["api_key"]},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "source_url", "expected_cost"),
    [
        ("text_to_image", None, 10),
        ("image_to_image", "https://example.com/source.png", 15),
        ("text_to_video", None, 50),
        ("image_to_video", "https://example.com/source.png", 60),
    ],
)
async def test_creates_all_generation_modes(
    client, kind: str, source_url: str | None, expected_cost: int
) -> None:
    test_client, _ = client
    user = await register_and_fund(test_client)
    payload = {"kind": kind, "prompt": "A test prompt"}
    if source_url is not None:
        payload["source_url"] = source_url

    response = await test_client.post(
        "/v1/generations",
        json=payload,
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": f"create-{kind}"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "created"
    assert response.json()["cost"] == expected_cost


@pytest.mark.asyncio
async def test_generation_cost_tracks_billable_parameters(client) -> None:
    test_client, _ = client
    user = await register_and_fund(test_client, amount=500)
    headers = {"X-API-Key": user["api_key"]}

    images = await test_client.post(
        "/v1/generations",
        json={
            "kind": "text_to_image",
            "prompt": "Four variants",
            "parameters": {"num_images": 4},
        },
        headers={**headers, "Idempotency-Key": "priced-images"},
    )
    video = await test_client.post(
        "/v1/generations",
        json={
            "kind": "image_to_video",
            "prompt": "Long video",
            "source_url": "https://example.com/source.png",
            "parameters": {"duration": 10, "resolution": "720p"},
        },
        headers={**headers, "Idempotency-Key": "priced-video"},
    )
    invalid = await test_client.post(
        "/v1/generations",
        json={
            "kind": "text_to_image",
            "prompt": "Too many",
            "parameters": {"num_images": 5},
        },
        headers={**headers, "Idempotency-Key": "invalid-price"},
    )
    metrics = await test_client.get("/metrics")

    assert images.status_code == 202
    assert images.json()["cost"] == 40
    assert video.status_code == 202
    assert video.json()["cost"] == 90
    assert invalid.status_code == 422
    assert (
        'content_service_generation_cost_tokens_total{kind="text_to_image",'
        'outcome="charged"} 40' in metrics.text
    )


@pytest.mark.asyncio
async def test_generation_creation_is_idempotent(client) -> None:
    test_client, _ = client
    user = await register_and_fund(test_client, amount=100)
    headers = {"X-API-Key": user["api_key"], "Idempotency-Key": "same-request"}
    payload = {"kind": "text_to_image", "prompt": "A lighthouse"}

    first = await test_client.post("/v1/generations", json=payload, headers=headers)
    duplicate = await test_client.post("/v1/generations", json=payload, headers=headers)
    conflicting = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "A different prompt"},
        headers=headers,
    )
    me = await test_client.get("/v1/me", headers={"X-API-Key": user["api_key"]})

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert first.json()["id"] == duplicate.json()["id"]
    assert conflicting.status_code == 409
    assert me.json()["balance"] == 90


@pytest.mark.asyncio
async def test_generation_requires_balance_and_valid_source(client) -> None:
    test_client, test_sessions = client
    user = (await test_client.post("/v1/auth")).json()
    headers = {"X-API-Key": user["api_key"], "Idempotency-Key": "no-money"}

    missing_source = await test_client.post(
        "/v1/generations",
        json={"kind": "image_to_video", "prompt": "Move"},
        headers=headers,
    )
    insufficient = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "No funds"},
        headers=headers,
    )

    assert missing_source.status_code == 422
    assert insufficient.status_code == 402
    async with test_sessions() as session:
        generations = (await session.scalars(select(GenerationModel))).all()
    assert generations == []


@pytest.mark.asyncio
async def test_worker_lifecycle_is_idempotent(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "Worker test"},
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "worker-test"},
    )
    generation_id = created.json()["id"]

    first_run = await process_generation_once(UUID(generation_id), test_sessions)
    duplicate_run = await process_generation_once(UUID(generation_id), test_sessions)
    result = await test_client.get(
        f"/v1/generations/{generation_id}",
        headers={"X-API-Key": user["api_key"]},
    )
    metrics = (await test_client.get("/metrics")).text

    assert first_run is True
    assert duplicate_run is False
    assert result.json()["status"] == "completed"
    assert result.json()["result"]["url"].endswith(".png")
    assert 'route="/v1/generations/{generation_id}"' in metrics
    metric_lines = [
        line for line in metrics.splitlines() if line.startswith("content_service_http_")
    ]
    assert all(generation_id not in line for line in metric_lines)


@pytest.mark.asyncio
async def test_expired_worker_lease_is_recovered_and_old_worker_is_fenced(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "Recover me"},
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "lease-test"},
    )
    generation_id = UUID(created.json()["id"])

    async with test_sessions() as session:
        first_claim = await SqlAlchemyGenerationRepository(
            session, processing_lease_seconds=-1
        ).start_processing(generation_id)
        assert first_claim is not None and first_claim.processing_token is not None
        outbox = await session.scalar(
            select(GenerationOutboxModel).where(
                GenerationOutboxModel.generation_id == generation_id
            )
        )
        assert outbox is not None
        outbox.processed_at = datetime.now(UTC)
        await session.commit()

    assert await recover_stale_generations_batch(session_factory=test_sessions) == 1

    async with test_sessions() as session:
        recovered = await session.get(GenerationModel, generation_id)
        outbox = await session.scalar(
            select(GenerationOutboxModel).where(
                GenerationOutboxModel.generation_id == generation_id
            )
        )
        assert recovered is not None and recovered.status == "queued"
        assert recovered.processing_token is None
        assert outbox is not None and outbox.processed_at is None

        second_claim = await SqlAlchemyGenerationRepository(session).start_processing(
            generation_id
        )
        assert second_claim is not None and second_claim.processing_token is not None
        repository = SqlAlchemyGenerationRepository(session)
        assert await repository.complete(
            generation_id,
            processing_token=first_claim.processing_token,
            result={"url": "https://example.invalid/stale.png"},
            provider="fake",
        ) is False
        assert await repository.complete(
            generation_id,
            processing_token=second_claim.processing_token,
            result={"url": "https://example.invalid/current.png"},
            provider="fake",
        ) is True


@pytest.mark.asyncio
async def test_outbox_dispatches_generation_only_once(client, monkeypatch) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "Outbox test"},
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "outbox-test"},
    )
    generation_id = UUID(created.json()["id"])
    dispatched_task_arguments: list[list[str]] = []

    def capture_task(*, args: list[str]) -> None:
        dispatched_task_arguments.append(args)

    monkeypatch.setattr(process_generation, "apply_async", capture_task)

    first_batch = await dispatch_outbox_batch(session_factory=test_sessions)
    second_batch = await dispatch_outbox_batch(session_factory=test_sessions)
    async with test_sessions() as session:
        generation = await session.get(GenerationModel, generation_id)
        outbox = await session.scalar(
            select(GenerationOutboxModel).where(
                GenerationOutboxModel.generation_id == generation_id
            )
        )

    assert first_batch == 1
    assert second_batch == 0
    assert dispatched_task_arguments == [[str(generation_id)]]
    assert generation is not None
    assert generation.status == "queued"
    assert outbox is not None
    assert outbox.processed_at is not None
    assert outbox.attempt_count == 1

    async with test_sessions() as session:
        generation = await session.get(GenerationModel, generation_id)
        assert generation is not None
        generation.updated_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()

    assert await recover_stale_generations_batch(session_factory=test_sessions) == 1
    assert await dispatch_outbox_batch(session_factory=test_sessions) == 1
    assert dispatched_task_arguments == [
        [str(generation_id)],
        [str(generation_id)],
    ]


@pytest.mark.asyncio
async def test_worker_reuses_provider_request_after_temporary_failure(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "Retry test"},
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "retry-test"},
    )
    generation_id = UUID(created.json()["id"])
    provider = FlakyProvider()

    with pytest.raises(TemporaryProviderError):
        await process_generation_once(generation_id, test_sessions, provider)

    async with test_sessions() as session:
        repository = SqlAlchemyGenerationRepository(session)
        stored = await session.get(GenerationModel, generation_id)
        assert stored is not None and stored.processing_token is not None
        assert await repository.requeue(
            generation_id,
            processing_token=stored.processing_token,
            error_code="temporary_provider_error",
            error_message="temporary outage",
        ) is True

    assert await process_generation_once(generation_id, test_sessions, provider) is True
    async with test_sessions() as session:
        stored = await session.get(GenerationModel, generation_id)

    assert provider.submit_count == 1
    assert provider.result_count == 2
    assert stored is not None
    assert stored.provider_request_id == "durable-request-id"
    assert stored.status == "completed"


@pytest.mark.asyncio
async def test_failed_generation_is_refunded_only_once(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "Refund test"},
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "refund-test"},
    )
    generation_id = UUID(created.json()["id"])

    async with test_sessions() as session:
        repository = SqlAlchemyGenerationRepository(session)
        processing = await repository.start_processing(generation_id)
        assert processing is not None and processing.processing_token is not None
        assert await repository.fail_and_refund(
            generation_id,
            processing_token=processing.processing_token,
            error_code="provider_error",
            error_message="provider failed",
        ) is True
        assert await repository.fail_and_refund(
            generation_id,
            processing_token=processing.processing_token,
            error_code="provider_error",
            error_message="duplicate failure",
        ) is False

    async with test_sessions() as session:
        stored_user = await session.get(UserModel, UUID(user["user_id"]))
        stored_generation = await session.get(GenerationModel, generation_id)
    metrics = (await test_client.get("/metrics")).text

    assert stored_user is not None
    assert stored_user.balance == 100
    assert stored_generation is not None
    assert stored_generation.status == "failed"
    assert stored_generation.refunded_at is not None
    assert (
        'content_service_generations{kind="text_to_image",status="failed"} 1'
        in metrics
    )
    assert (
        'content_service_generation_cost_tokens_total{kind="text_to_image",'
        'outcome="refunded"} 10' in metrics
    )


@pytest.mark.asyncio
async def test_generation_can_switch_provider_before_request_was_submitted(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={"kind": "text_to_image", "prompt": "Failover test"},
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "failover-test"},
    )
    generation_id = UUID(created.json()["id"])

    with pytest.raises(TemporaryProviderError):
        await process_generation_once(
            generation_id,
            test_sessions,
            UnavailableBeforeSubmissionProvider(),
        )

    async with test_sessions() as session:
        repository = SqlAlchemyGenerationRepository(session)
        assert await repository.get_provider_request_id(generation_id) is None
        stored = await session.get(GenerationModel, generation_id)
        assert stored is not None and stored.processing_token is not None
        assert await repository.requeue(
            generation_id,
            processing_token=stored.processing_token,
            error_code="provider_failover",
            error_message="switching provider",
        ) is True

    assert (
        await process_generation_once(
            generation_id,
            test_sessions,
            FakeGenerationProvider(),
        )
        is True
    )
    async with test_sessions() as session:
        stored = await session.get(GenerationModel, generation_id)

    assert stored is not None
    assert stored.status == "completed"
    assert stored.provider == "fake"


@pytest.mark.asyncio
async def test_completed_generation_callback_payload(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={
            "kind": "text_to_image",
            "prompt": "Callback test",
            "callback_url": "https://client.example/webhooks/generations",
        },
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "callback-test"},
    )
    generation_id = UUID(created.json()["id"])
    assert await process_generation_once(generation_id, test_sessions) is True
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["headers"] = request.headers
        received["payload"] = json.loads(request.content)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        delivered = await deliver_callback_once(
            generation_id,
            delivery_id="delivery-123",
            session_factory=test_sessions,
            client=http_client,
        )

    assert delivered is True
    assert received["headers"]["X-Webhook-Event"] == "generation.completed"
    assert received["headers"]["X-Webhook-Delivery"] == "delivery-123"
    assert received["payload"]["generation_id"] == str(generation_id)
    assert received["payload"]["status"] == "completed"
    assert received["payload"]["result"]["url"].endswith(".png")
    assert received["payload"]["error"] is None


@pytest.mark.asyncio
async def test_callback_outbox_persists_retry_and_delivery(client, monkeypatch) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={
            "kind": "text_to_image",
            "prompt": "Durable callback",
            "callback_url": "https://client.example/webhook",
        },
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "callback-outbox"},
    )
    generation_id = UUID(created.json()["id"])
    assert await process_generation_once(generation_id, test_sessions) is True

    async with test_sessions() as session:
        event = await session.scalar(
            select(CallbackOutboxModel).where(
                CallbackOutboxModel.generation_id == generation_id
            )
        )
        assert event is not None
        event_id = event.id
        assert event.attempt_count == 0

    dispatched: list[str] = []

    def capture_callback(*, args: list[str]) -> None:
        dispatched.extend(args)

    monkeypatch.setattr(deliver_generation_callback, "apply_async", capture_callback)
    assert await dispatch_callback_outbox_batch(session_factory=test_sessions) == 1
    assert dispatched == [str(event_id)]

    first_claim = await claim_callback_delivery(event_id, test_sessions)
    assert first_claim is not None
    assert await finish_callback_delivery(
        event_id,
        first_claim[1],
        delivered=False,
        error_message="HTTP 503",
        session_factory=test_sessions,
    )

    async with test_sessions() as session:
        event = await session.get(CallbackOutboxModel, event_id)
        assert event is not None
        assert event.attempt_count == 1
        assert event.delivered_at is None
        assert event.last_error == "HTTP 503"
        event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    second_claim = await claim_callback_delivery(event_id, test_sessions)
    assert second_claim is not None
    assert await finish_callback_delivery(
        event_id,
        second_claim[1],
        delivered=True,
        error_message=None,
        session_factory=test_sessions,
    )
    async with test_sessions() as session:
        event = await session.get(CallbackOutboxModel, event_id)
        assert event is not None
        assert event.attempt_count == 2
        assert event.delivered_at is not None


@pytest.mark.asyncio
async def test_callback_outbox_stops_after_five_attempts(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={
            "kind": "text_to_image",
            "prompt": "Five callback attempts",
            "callback_url": "https://client.example/webhook",
        },
        headers={"X-API-Key": user["api_key"], "Idempotency-Key": "callback-five"},
    )
    generation_id = UUID(created.json()["id"])
    assert await process_generation_once(generation_id, test_sessions) is True
    async with test_sessions() as session:
        event = await session.scalar(
            select(CallbackOutboxModel).where(
                CallbackOutboxModel.generation_id == generation_id
            )
        )
        assert event is not None
        event_id = event.id

    for attempt in range(CALLBACK_MAX_ATTEMPTS):
        claim = await claim_callback_delivery(event_id, test_sessions)
        assert claim is not None
        assert await finish_callback_delivery(
            event_id,
            claim[1],
            delivered=False,
            error_message="HTTP 503",
            session_factory=test_sessions,
        )
        async with test_sessions() as session:
            event = await session.get(CallbackOutboxModel, event_id)
            assert event is not None
            assert event.attempt_count == attempt + 1
            event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    assert await claim_callback_delivery(event_id, test_sessions) is None


def test_callback_task_records_success_and_failure(monkeypatch) -> None:
    generation_id = uuid4()
    claim_token = uuid4()
    finish_calls: list[tuple[bool, str | None]] = []

    async def claim(event_id: UUID):
        return generation_id, claim_token

    async def deliver(generation_id: UUID, *, delivery_id: str) -> bool:
        return True

    async def finish(
        event_id: UUID,
        token: UUID,
        *,
        delivered: bool,
        error_message: str | None,
    ) -> bool:
        assert token == claim_token
        finish_calls.append((delivered, error_message))
        return True

    monkeypatch.setattr(worker_tasks, "_claim_callback_with_worker_engine", claim)
    monkeypatch.setattr(worker_tasks, "_deliver_callback_with_worker_engine", deliver)
    monkeypatch.setattr(worker_tasks, "_finish_callback_with_worker_engine", finish)

    assert deliver_generation_callback.run(str(uuid4())) is True

    async def failed_delivery(generation_id: UUID, *, delivery_id: str) -> bool:
        raise CallbackDeliveryError("HTTP 503")

    monkeypatch.setattr(
        worker_tasks, "_deliver_callback_with_worker_engine", failed_delivery
    )
    assert deliver_generation_callback.run(str(uuid4())) is False
    assert finish_calls == [(True, None), (False, "HTTP 503")]


@pytest.mark.asyncio
async def test_callback_http_error_is_retryable(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={
            "kind": "text_to_image",
            "prompt": "Callback retry test",
            "callback_url": "https://client.example/webhook",
        },
        headers={
            "X-API-Key": user["api_key"],
            "Idempotency-Key": "callback-retry-test",
        },
    )
    generation_id = UUID(created.json()["id"])
    assert await process_generation_once(generation_id, test_sessions) is True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(CallbackDeliveryError, match="HTTP 503"):
            await deliver_callback_once(
                generation_id,
                delivery_id="delivery-503",
                session_factory=test_sessions,
                client=http_client,
            )

    assert CALLBACK_MAX_ATTEMPTS == 5
    assert CALLBACK_RETRY_DELAY_SECONDS == 15


@pytest.mark.asyncio
async def test_failed_generation_callback_contains_error(client) -> None:
    test_client, test_sessions = client
    user = await register_and_fund(test_client, amount=100)
    created = await test_client.post(
        "/v1/generations",
        json={
            "kind": "text_to_image",
            "prompt": "Failed callback test",
            "callback_url": "https://client.example/webhook",
        },
        headers={
            "X-API-Key": user["api_key"],
            "Idempotency-Key": "failed-callback-test",
        },
    )
    generation_id = UUID(created.json()["id"])
    async with test_sessions() as session:
        repository = SqlAlchemyGenerationRepository(session)
        processing = await repository.start_processing(generation_id)
        assert processing is not None and processing.processing_token is not None
        assert await repository.fail_and_refund(
            generation_id,
            processing_token=processing.processing_token,
            error_code="provider_error",
            error_message="Fal rejected request",
        )
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received.update(json.loads(request.content))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        assert await deliver_callback_once(
            generation_id,
            delivery_id="failed-delivery",
            session_factory=test_sessions,
            client=http_client,
        )

    assert received["event"] == "generation.failed"
    assert received["result"] is None
    assert received["error"] == {
        "code": "provider_error",
        "message": "Fal rejected request",
    }
