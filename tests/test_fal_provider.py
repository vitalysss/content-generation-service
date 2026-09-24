import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.application.providers import PermanentProviderError, TemporaryProviderError
from app.domain.generation import Generation, GenerationKind, GenerationStatus
from app.infrastructure.fal_provider import FalGenerationProvider


def make_generation(kind: GenerationKind) -> Generation:
    now = datetime.now(UTC)
    return Generation(
        id=uuid4(),
        user_id=uuid4(),
        kind=kind,
        status=GenerationStatus.PROCESSING,
        prompt="A cinematic prompt",
        source_url=(
            "https://example.com/source.png"
            if kind in {GenerationKind.IMAGE_TO_IMAGE, GenerationKind.IMAGE_TO_VIDEO}
            else None
        ),
        callback_url=None,
        input_params={"seed": 42},
        result=None,
        error_code=None,
        error_message=None,
        cost=10,
        provider=None,
        provider_request_id=None,
        processing_token=None,
        processing_started_at=None,
        lease_expires_at=None,
        attempt_count=0,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    ("kind", "expected_path", "source_field"),
    [
        (GenerationKind.TEXT_TO_IMAGE, "/fal-ai/wan-25-preview/text-to-image", None),
        (GenerationKind.IMAGE_TO_IMAGE, "/fal-ai/wan-25-preview/image-to-image", "image_urls"),
        (GenerationKind.TEXT_TO_VIDEO, "/fal-ai/wan-25-preview/text-to-video", None),
        (GenerationKind.IMAGE_TO_VIDEO, "/fal-ai/wan-25-preview/image-to-video", "image_url"),
    ],
)
@pytest.mark.asyncio
async def test_fal_queue_protocol(
    kind: GenerationKind, expected_path: str, source_field: str | None
) -> None:
    submitted_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Key test-key"
        if request.method == "POST":
            assert request.url.path == expected_path
            submitted_payload.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "request_id": "request-1",
                    "status_url": "https://queue.fal.run/status",
                    "response_url": "https://queue.fal.run/result",
                },
            )
        if request.url.path.endswith("/requests/request-1/status"):
            return httpx.Response(200, json={"status": "COMPLETED"})
        return httpx.Response(200, json={"images": [{"url": "https://cdn.test/out.png"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FalGenerationProvider(api_key="test-key", poll_interval=0, client=client)
        generation = make_generation(kind)
        request_id = await provider.submit(generation)
        result = await provider.get_result(generation, request_id)

    assert submitted_payload["prompt"] == "A cinematic prompt"
    assert submitted_payload["seed"] == 42
    if source_field == "image_urls":
        assert submitted_payload[source_field] == ["https://example.com/source.png"]
    elif source_field == "image_url":
        assert submitted_payload[source_field] == "https://example.com/source.png"
    else:
        assert "image_url" not in submitted_payload
        assert "image_urls" not in submitted_payload
    assert result.provider == "fal"
    assert result.data["images"][0]["url"] == "https://cdn.test/out.png"


def test_fal_provider_requires_key() -> None:
    with pytest.raises(ValueError, match="FAL_KEY"):
        FalGenerationProvider(api_key="")


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (429, TemporaryProviderError),
        (503, TemporaryProviderError),
        (400, PermanentProviderError),
        (401, PermanentProviderError),
    ],
)
@pytest.mark.asyncio
async def test_fal_classifies_http_errors(
    status_code: int, expected_error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FalGenerationProvider(api_key="test-key", client=client)
        with pytest.raises(expected_error, match=f"HTTP {status_code}"):
            await provider.submit(make_generation(GenerationKind.TEXT_TO_IMAGE))


@pytest.mark.asyncio
async def test_fal_rejects_invalid_submit_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FalGenerationProvider(api_key="test-key", client=client)
        with pytest.raises(PermanentProviderError, match="invalid submit response"):
            await provider.submit(make_generation(GenerationKind.TEXT_TO_IMAGE))


@pytest.mark.asyncio
async def test_fal_failed_status_is_permanent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "FAILED"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FalGenerationProvider(api_key="test-key", client=client)
        with pytest.raises(PermanentProviderError, match="status FAILED"):
            await provider.get_result(
                make_generation(GenerationKind.TEXT_TO_IMAGE), "request-failed"
            )


@pytest.mark.asyncio
async def test_fal_poll_timeout_is_temporary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "IN_PROGRESS"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FalGenerationProvider(
            api_key="test-key",
            client=client,
            poll_interval=0,
            request_timeout=-1,
        )
        with pytest.raises(TemporaryProviderError, match="timed out"):
            await provider.get_result(
                make_generation(GenerationKind.TEXT_TO_VIDEO), "request-timeout"
            )
