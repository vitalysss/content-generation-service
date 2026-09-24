from app.application.providers import ProviderResult
from app.domain.generation import Generation


class FakeGenerationProvider:
    name = "fake"

    async def submit(self, generation: Generation) -> str:
        return str(generation.id)

    async def get_result(
        self, generation: Generation, request_id: str
    ) -> ProviderResult:
        extension = "mp4" if generation.kind.value.endswith("video") else "png"
        return ProviderResult(
            provider=self.name,
            data={
                "url": f"https://example.invalid/generated/{generation.id}.{extension}",
                "kind": generation.kind.value,
                "request_id": request_id,
            },
        )
