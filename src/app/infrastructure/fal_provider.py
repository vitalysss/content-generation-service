import asyncio
import time
from typing import Any

import httpx

from app.application.providers import (
    PermanentProviderError,
    ProviderResult,
    TemporaryProviderError,
)
from app.domain.generation import Generation, GenerationKind

MODEL_IDS = {
    GenerationKind.TEXT_TO_IMAGE: "fal-ai/wan-25-preview/text-to-image",
    GenerationKind.IMAGE_TO_IMAGE: "fal-ai/wan-25-preview/image-to-image",
    GenerationKind.TEXT_TO_VIDEO: "fal-ai/wan-25-preview/text-to-video",
    GenerationKind.IMAGE_TO_VIDEO: "fal-ai/wan-25-preview/image-to-video",
}


class FalGenerationProvider:
    name = "fal"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://queue.fal.run",
        poll_interval: float = 2.0,
        request_timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("FAL_KEY is required when GENERATION_PROVIDER=fal")
        self._base_url = base_url.rstrip("/")
        self._poll_interval = poll_interval
        self._request_timeout = request_timeout
        self._client = client
        self._headers = {"Authorization": f"Key {api_key}"}

    def build_input(self, generation: Generation) -> dict[str, Any]:
        payload = dict(generation.input_params)
        payload["prompt"] = generation.prompt
        if generation.kind == GenerationKind.IMAGE_TO_IMAGE:
            payload["image_urls"] = [generation.source_url]
        elif generation.kind == GenerationKind.IMAGE_TO_VIDEO:
            payload["image_url"] = generation.source_url
        return payload

    async def submit(self, generation: Generation) -> str:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            model_id = MODEL_IDS[generation.kind]
            try:
                submitted = await client.post(
                    f"{self._base_url}/{model_id}",
                    headers=self._headers,
                    json=self.build_input(generation),
                )
                self._raise_for_status(submitted)
                return str(submitted.json()["request_id"])
            except (httpx.TimeoutException, httpx.TransportError) as error:
                raise TemporaryProviderError(str(error)) from error
            except (KeyError, ValueError) as error:
                raise PermanentProviderError("Fal returned an invalid submit response") from error
        finally:
            if owns_client:
                await client.aclose()

    async def get_result(
        self, generation: Generation, request_id: str
    ) -> ProviderResult:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30.0)
        model_id = MODEL_IDS[generation.kind]
        request_url = f"{self._base_url}/{model_id}/requests/{request_id}"
        try:
            deadline = time.monotonic() + self._request_timeout
            while True:
                status_response = await client.get(
                    f"{request_url}/status", headers=self._headers
                )
                self._raise_for_status(status_response)
                provider_status = status_response.json()["status"]
                if provider_status == "COMPLETED":
                    break
                if provider_status not in {"IN_QUEUE", "IN_PROGRESS"}:
                    raise PermanentProviderError(
                        f"Fal request failed with status {provider_status}"
                    )
                if time.monotonic() >= deadline:
                    raise TemporaryProviderError("Fal request timed out")
                await asyncio.sleep(self._poll_interval)

            response = await client.get(request_url, headers=self._headers)
            self._raise_for_status(response)
            return ProviderResult(data=response.json(), provider=self.name)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise TemporaryProviderError(str(error)) from error
        except (KeyError, ValueError) as error:
            raise PermanentProviderError("Fal returned an invalid status response") from error
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        message = f"Fal returned HTTP {response.status_code}"
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            raise TemporaryProviderError(message)
        raise PermanentProviderError(message)
