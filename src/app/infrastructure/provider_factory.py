from app.application.providers import GenerationProvider
from app.infrastructure.fake_provider import FakeGenerationProvider
from app.infrastructure.fal_provider import FalGenerationProvider
from app.settings import Settings


def create_generation_provider(
    settings: Settings, provider_name: str | None = None
) -> GenerationProvider:
    selected_provider = provider_name or settings.generation_provider
    if selected_provider == "fake":
        return FakeGenerationProvider()
    if selected_provider == "fal":
        return FalGenerationProvider(
            api_key=settings.fal_key.get_secret_value(),
            base_url=settings.fal_queue_base_url,
            poll_interval=settings.fal_poll_interval_seconds,
            request_timeout=settings.fal_request_timeout_seconds,
        )
    raise ValueError(f"Unknown generation provider: {selected_provider}")
